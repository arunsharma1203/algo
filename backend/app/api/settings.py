from fastapi import APIRouter
from pydantic import BaseModel
import sqlite3
from app.data.historical_data_layer import get_db_path

router = APIRouter()

class TelegramConfig(BaseModel):
    bot_token: str
    chat_id: str

@router.post("/telegram")
async def save_telegram_config(config: TelegramConfig):
    conn = sqlite3.connect(get_db_path(), timeout=5.0)
    conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('telegram_bot_token', ?)", (config.bot_token,))
    conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('telegram_chat_id', ?)", (config.chat_id,))
    conn.commit()
    conn.close()
    return {"status": "success", "message": "Telegram configuration saved."}

@router.post("/telegram/test")
async def test_telegram_config(config: TelegramConfig):
    try:
        from app.analytics.telegram_notifier import send_telegram_message
        import requests
        
        # We temporarily bypass the DB to test the provided config directly
        bot_token = config.bot_token
        chat_id = config.chat_id
        
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": "✅ <b>AI Trading System Connected!</b>\n\nYour Autonomous Bot is now successfully linked to this chat. You will receive live trade alerts and early exit warnings here.",
            "parse_mode": "HTML"
        }
        
        response = requests.post(url, json=payload, timeout=5)
        if response.status_code == 200:
            return {"status": "success", "message": "Test message sent successfully!"}
        else:
            return {"status": "error", "message": f"Telegram API Error: {response.text}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.get("/telegram")
async def get_telegram_config():
    conn = sqlite3.connect(get_db_path(), timeout=5.0)
    cur = conn.cursor()
    cur.execute("SELECT value FROM app_settings WHERE key = 'telegram_bot_token'")
    token = cur.fetchone()
    cur.execute("SELECT value FROM app_settings WHERE key = 'telegram_chat_id'")
    chat = cur.fetchone()
    conn.close()
    return {
        "bot_token": token[0] if token else "",
        "chat_id": chat[0] if chat else ""
    }

class AutopilotConfig(BaseModel):
    enabled: bool = True
    min_conviction: float = 70.0

@router.get("/autopilot")
async def get_autopilot_config():
    conn = sqlite3.connect(get_db_path(), timeout=5.0)
    cur = conn.cursor()
    cur.execute("SELECT value FROM app_settings WHERE key = 'autopilot_enabled'")
    en = cur.fetchone()
    cur.execute("SELECT value FROM app_settings WHERE key = 'autopilot_min_conviction'")
    conv = cur.fetchone()
    conn.close()
    return {
        "enabled": False if en and en[0] == 'false' else True,
        "min_conviction": float(conv[0]) if conv else 70.0,
        "schedule": ["09:30 IST", "11:30 IST", "13:30 IST"]
    }

@router.post("/autopilot")
async def save_autopilot_config(config: AutopilotConfig):
    conn = sqlite3.connect(get_db_path(), timeout=5.0)
    conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('autopilot_enabled', ?)", ('true' if config.enabled else 'false',))
    conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('autopilot_min_conviction', ?)", (str(config.min_conviction),))
    conn.commit()
    conn.close()
    return {"status": "success", "message": f"Autopilot set to {'Enabled' if config.enabled else 'Disabled'}."}

@router.post("/autopilot/trigger")
def trigger_autopilot_manual(session: str = "Manual Trigger"):
    from app.tasks.autopilot_scanner import run_scheduled_autopilot_sweep
    import threading
    threading.Thread(target=run_scheduled_autopilot_sweep, args=(session,), daemon=True).start()
    return {"status": "success", "message": f"Autopilot sweep '{session}' dispatched in background."}

class HeatCapConfig(BaseModel):
    max_heat_cap_pct: float = 6.0

@router.get("/portfolio-heat")
def get_heat_cap_setting():
    conn = sqlite3.connect(get_db_path(), timeout=5.0)
    cur = conn.execute("SELECT value FROM app_settings WHERE key = 'portfolio_max_heat_cap'")
    row = cur.fetchone()
    conn.close()
    return {"max_heat_cap_pct": float(row[0]) if row else 6.0}

@router.post("/portfolio-heat")
def save_heat_cap_setting(config: HeatCapConfig):
    conn = sqlite3.connect(get_db_path(), timeout=5.0)
    conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('portfolio_max_heat_cap', ?)", (str(config.max_heat_cap_pct),))
    conn.commit()
    conn.close()
    return {"status": "success", "max_heat_cap_pct": config.max_heat_cap_pct}

# -------------------------------------------------------------
# SIMULATION MODE & EXECUTION SAFEGUARDS
# -------------------------------------------------------------

class SimulationConfig(BaseModel):
    simulation_mode: bool = True

@router.get("/simulation")
def get_simulation_mode():
    conn = sqlite3.connect(get_db_path(), timeout=5.0)
    cur = conn.cursor()
    cur.execute("SELECT value FROM app_settings WHERE key = 'simulation_mode'")
    row = cur.fetchone()
    conn.close()
    # Default is True (Paper Trading) for safety
    is_simulation = True if not row or row[0] != 'false' else False
    return {"simulation_mode": is_simulation}

@router.post("/simulation")
def save_simulation_mode(config: SimulationConfig):
    conn = sqlite3.connect(get_db_path(), timeout=5.0)
    conn.execute(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('simulation_mode', ?)",
        ('true' if config.simulation_mode else 'false',)
    )
    conn.commit()
    conn.close()
    mode_name = "Paper Trading Simulation" if config.simulation_mode else "Live Real-Money Order Routing"
    return {
        "status": "success",
        "simulation_mode": config.simulation_mode,
        "message": f"Execution environment switched to {mode_name}."
    }

# -------------------------------------------------------------
# MARKET DATA SOURCE & UPSTOX INTEGRATION ENDPOINTS
# -------------------------------------------------------------

class DataSourceConfig(BaseModel):
    source: str = 'upstox'

class UpstoxConfig(BaseModel):
    api_key: str = ""
    api_secret: str = ""
    access_token: str = ""      # Market Data (Read-Only) Token
    market_token: str = ""      # Market Data (Read-Only) Token
    algo_token: str = ""        # Algo Trading (Read + Trade) Token
    test_type: str = "market"   # 'market' or 'algo'
    redirect_uri: str = "http://localhost:8000/api/settings/upstox/callback"

@router.get("/datasource")
def get_datasource_setting():
    conn = sqlite3.connect(get_db_path(), timeout=5.0)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)")
    cur.execute("SELECT value FROM app_settings WHERE key = 'market_data_source'")
    row = cur.fetchone()
    cur.execute("SELECT value FROM app_settings WHERE key = 'upstox_access_token'")
    token_row = cur.fetchone()
    cur.execute("SELECT value FROM app_settings WHERE key = 'upstox_algo_token'")
    algo_row = cur.fetchone()
    conn.close()
    
    has_upstox_token = bool(token_row and token_row[0] and len(token_row[0].strip()) > 10)
    has_algo_token = bool(algo_row and algo_row[0] and len(algo_row[0].strip()) > 10)
    
    return {
        "source": "upstox",
        "has_upstox_token": has_upstox_token,
        "has_algo_token": has_algo_token,
        "supported_sources": ["upstox"]
    }

@router.post("/datasource")
def save_datasource_setting(config: DataSourceConfig):
    chosen_source = 'upstox'
    conn = sqlite3.connect(get_db_path(), timeout=5.0)
    conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('market_data_source', ?)", (chosen_source,))
    conn.commit()
    conn.close()
    return {"status": "success", "source": chosen_source, "message": "Authoritative market data source locked strictly to UPSTOX."}

@router.get("/upstox/token-status")
def get_upstox_token_status_endpoint(verify_live: bool = False):
    """
    Returns the real-time health and expiration status of the Upstox market data token.
    Used by frontend banner to prompt token re-entry and lock execution on auth failure.
    """
    from app.data.upstox_provider import get_upstox_token_status
    conn = sqlite3.connect(get_db_path(), timeout=5.0)
    cur = conn.cursor()
    cur.execute("SELECT value FROM app_settings WHERE key = 'upstox_auth_status'")
    row_status = cur.fetchone()
    cur.execute("SELECT value FROM app_settings WHERE key = 'upstox_auth_error'")
    row_err = cur.fetchone()
    cur.execute("SELECT value FROM app_settings WHERE key = 'upstox_auth_error_time'")
    row_time = cur.fetchone()
    conn.close()

    token_status = get_upstox_token_status(verify_live=verify_live)
    db_auth_status = row_status[0] if row_status else None

    # If DB has flagged TOKEN_EXPIRED or live probe failed, mark invalid
    if db_auth_status == "TOKEN_EXPIRED":
        token_status["status"] = "TOKEN_EXPIRED"
        token_status["is_valid"] = False
        token_status["message"] = row_err[0] if row_err else "Upstox token expired or authentication rejected."
        token_status["error_time"] = row_time[0] if row_time else None

    return token_status

@router.get("/upstox")
def get_upstox_settings():
    from app.data.upstox_provider import get_upstox_config, get_upstox_limiter
    cfg = get_upstox_config()
    masked_secret = f"{cfg['api_secret'][:4]}****{cfg['api_secret'][-4:]}" if len(cfg['api_secret']) > 8 else ("****" if cfg['api_secret'] else "")
    limiter = get_upstox_limiter()
    return {
        "api_key": cfg["api_key"],
        "api_secret_masked": masked_secret,
        "has_secret": bool(cfg["api_secret"]),
        "has_token": bool(cfg["access_token"]),
        "has_market_token": bool(cfg["access_token"]),
        "has_algo_token": bool(cfg.get("algo_token")),
        "market_token": cfg["access_token"],
        "algo_token": cfg.get("algo_token", ""),
        "redirect_uri": cfg["redirect_uri"],
        "rate_limits": limiter.get_stats()
    }

@router.post("/upstox")
def save_upstox_settings(config: UpstoxConfig):
    conn = sqlite3.connect(get_db_path(), timeout=5.0)
    if config.api_key:
        conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('upstox_api_key', ?)", (config.api_key.strip(),))
    if config.api_secret:
        conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('upstox_api_secret', ?)", (config.api_secret.strip(),))
    
    # Save Market Data Token (access_token or market_token)
    market_tok = config.market_token.strip() if config.market_token else (config.access_token.strip() if config.access_token else "")
    if market_tok:
        conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('upstox_access_token', ?)", (market_tok,))
        conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('upstox_market_token', ?)", (market_tok,))
        # Reset auth failure status upon new token configuration
        conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('upstox_auth_status', 'ACTIVE')")
        conn.execute("DELETE FROM app_settings WHERE key IN ('upstox_auth_error', 'upstox_auth_error_time')")
        conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('market_data_source', 'upstox')")
        
    # Save Algo Trading Token
    if config.algo_token:
        conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('upstox_algo_token', ?)", (config.algo_token.strip(),))
        
    if config.redirect_uri:
        conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('upstox_redirect_uri', ?)", (config.redirect_uri.strip(),))
    conn.commit()
    conn.close()
    return {"status": "success", "message": "Upstox configuration saved and active."}

@router.post("/upstox/test")
def test_upstox_api(config: UpstoxConfig):
    from app.data.upstox_provider import test_upstox_connection
    test_type = config.test_type if config.test_type in ("market", "algo") else "market"
    token_to_test = None
    if test_type == "algo":
        token_to_test = config.algo_token if config.algo_token else None
    else:
        token_to_test = config.market_token if config.market_token else (config.access_token if config.access_token else None)
        
    result = test_upstox_connection(token_to_test, token_type=test_type)
    
    # If market data connection was verified successfully, automatically switch active data source to Upstox
    if result.get("status") == "success" and test_type == "market":
        try:
            conn = sqlite3.connect(get_db_path(), timeout=5.0)
            conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('market_data_source', 'upstox')")
            conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('upstox_auth_status', 'ACTIVE')")
            conn.execute("DELETE FROM app_settings WHERE key IN ('upstox_auth_error', 'upstox_auth_error_time')")
            if token_to_test:
                conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('upstox_access_token', ?)", (token_to_test.strip(),))
            conn.commit()
            conn.close()
            result["auto_switched_source"] = "upstox"
            result["message"] += " Live Market Data Engine automatically set to UPSTOX and status ACTIVE."
        except Exception as e:
            pass
            
    return result

@router.get("/upstox/auth-url")
def get_upstox_auth_url():
    from app.data.upstox_provider import get_upstox_config
    cfg = get_upstox_config()
    api_key = cfg.get("api_key", "")
    redirect_uri = cfg.get("redirect_uri", "http://localhost:8000/api/settings/upstox/callback")
    
    if not api_key:
        return {"status": "error", "message": "Upstox API Key (Client ID) is required to generate Login URL."}
        
    auth_url = f"https://api.upstox.com/v2/login/authorization/dialog?response_type=code&client_id={api_key}&redirect_uri={redirect_uri}"
    return {"status": "success", "auth_url": auth_url, "redirect_uri": redirect_uri}


@router.get("/upstox/automator-status")
def get_upstox_automator_status():
    """Returns isolated Upstox Token Automator telemetry, live 6-digit TOTP (if configured), and sync status."""
    from app.data.upstox_token_automator import UpstoxTokenAutomator
    return UpstoxTokenAutomator.get_automator_status()


@router.post("/upstox/automator-config")
def save_upstox_automator_config(payload: dict):
    """Saves isolated Upstox Token Automator configuration (sync_both_slots, optional local TOTP secret)."""
    from app.data.upstox_token_automator import UpstoxTokenAutomator
    return UpstoxTokenAutomator.save_automator_config(payload or {})


@router.post("/upstox/smart-activate")
def smart_activate_upstox_token(payload: dict):
    """
    Accepts a callback URL (?code=...), an OAuth code, or a raw JWT access_token,
    exchanges if needed, and activates across both Market Data & Algo Trading slots.
    """
    from app.data.upstox_token_automator import UpstoxTokenAutomator
    raw_input = str((payload or {}).get("raw_input", ""))
    sync_both = (payload or {}).get("sync_both_slots", None)
    return UpstoxTokenAutomator.smart_exchange_or_activate(raw_input, sync_both_slots=sync_both)


@router.post("/upstox/request-mobile-approval")
def request_upstox_v3_mobile_approval():
    """Triggers Upstox V3 Token Request API for 1-Tap approval on Upstox Mobile App / WhatsApp."""
    from app.data.upstox_token_automator import UpstoxTokenAutomator
    return UpstoxTokenAutomator.request_v3_mobile_approval()


@router.post("/upstox/webhook")
def receive_upstox_v3_token_webhook(payload: dict):
    """
    Webhook Notifier endpoint for Upstox V3 Automated Token delivery.
    When the user approves the token request on their phone, Upstox POSTs `access_token` here.
    """
    from app.data.upstox_token_automator import UpstoxTokenAutomator
    tok = str((payload or {}).get("access_token") or (payload or {}).get("data", {}).get("access_token") or "")
    user_name = str((payload or {}).get("user_name") or (payload or {}).get("user_id") or "Upstox Webhook")
    if tok:
        return UpstoxTokenAutomator.activate_and_persist_token(
            access_token=tok,
            sync_both_slots=True,
            user_name=user_name,
            method_label="Upstox V3 Mobile Approval Webhook",
        )
    return {"status": "ignored", "message": "No access_token in webhook payload."}


@router.get("/upstox/callback")
def upstox_oauth_callback(code: str = ""):
    """
    1-Click OAuth Callback Handler:
    Exchanges authorization `code` with Upstox API v2, atomically syncs BOTH
    `upstox_access_token` (`upstox_market_token`) and `upstox_algo_token`,
    clears any `TOKEN_EXPIRED` or rate-limit lockouts, hot-reloads the WebSocket V3 stream,
    notifies the parent React window via `window.opener.postMessage`, and auto-closes the popup!
    """
    from fastapi.responses import HTMLResponse
    import json
    from app.data.upstox_token_automator import UpstoxTokenAutomator

    if not code:
        return HTMLResponse("<h3>❌ Authorization failed: No auth code received from Upstox.</h3>", status_code=400)

    result = UpstoxTokenAutomator.exchange_oauth_code(code, method_label="1-Click OAuth Popup")
    if result.get("status") == "success":
        user_name = result.get("user_name", "Upstox Trader")
        slots_str = " &amp; ".join(result.get("slots_updated", ["Market Data", "Algo Trading"]))
        payload_json = json.dumps({
            "type": "UPSTOX_OAUTH_SUCCESS",
            "user_name": user_name,
            "refreshed_at_ist": result.get("refreshed_at_ist"),
            "expires_at_ist": result.get("expires_at_ist"),
        })
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Upstox 1-Click Connected</title>
            <style>
                body {{ font-family: system-ui, -apple-system, sans-serif; background: #020617; color: #f8fafc; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }}
                .card {{ background: #0f172a; padding: 2.25rem; border-radius: 1.25rem; border: 1px solid #10b981; text-align: center; max-width: 440px; box-shadow: 0 20px 30px -5px rgba(0,0,0,0.7); }}
                .badge {{ display: inline-block; background: rgba(16,185,129,0.15); color: #34d399; border: 1px solid rgba(16,185,129,0.4); font-size: 0.75rem; font-weight: 800; padding: 0.25rem 0.75rem; border-radius: 999px; margin-bottom: 0.75rem; }}
                .btn {{ display: inline-block; margin-top: 1.25rem; padding: 0.65rem 1.4rem; background: #10b981; color: #ffffff; text-decoration: none; border-radius: 0.6rem; font-weight: 700; font-size: 0.85rem; cursor: pointer; border: none; }}
            </style>
        </head>
        <body>
            <div class="card">
                <div class="badge">⚡ ZERO COPY-PASTE SYNC COMPLETE</div>
                <h2 style="color: #10b981; margin: 0 0 0.5rem 0;">✅ Upstox Connected!</h2>
                <p style="margin: 0.25rem 0; font-size: 0.92rem;">Authenticated as <strong>{user_name}</strong></p>
                <p style="color: #94a3b8; font-size: 0.8rem; margin-top: 0.5rem;">
                    Automatically synced: <strong style="color: #e2e8f0;">{slots_str}</strong><br/>
                    WebSocket V3 Stream &amp; Rate Limiter Active.
                </p>
                <p style="color: #6ee7b7; font-size: 0.75rem; margin-top: 0.9rem;">Closing window automatically...</p>
                <button class="btn" onclick="window.close(); window.location.href='http://localhost:5173/profile';">Return to Trading Desk</button>
            </div>
            <script>
                (function() {{
                    var msg = {payload_json};
                    try {{
                        if (window.opener && !window.opener.closed) {{
                            window.opener.postMessage(msg, '*');
                            setTimeout(function() {{ window.close(); }}, 1100);
                        }} else {{
                            setTimeout(function() {{ window.location.href = 'http://localhost:5173/profile'; }}, 1500);
                        }}
                    }} catch (e) {{}}
                }})();
            </script>
        </body>
        </html>
        """
        return HTMLResponse(content=html_content)

    err_msg = result.get("message", "Unknown token exchange failure")
    return HTMLResponse(
        f"<div style='font-family:sans-serif;padding:2rem;background:#0f172a;color:#f8fafc;height:100vh;'>"
        f"<h3 style='color:#f43f5e;'>❌ Upstox 1-Click Token Exchange Failed</h3>"
        f"<pre style='background:#1e293b;padding:1rem;border-radius:0.5rem;color:#fda4af;white-space:pre-wrap;'>{err_msg}</pre>"
        f"</div>",
        status_code=400,
    )



