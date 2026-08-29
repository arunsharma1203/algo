from fastapi import APIRouter
from pydantic import BaseModel
import sqlite3

router = APIRouter()

class TelegramConfig(BaseModel):
    bot_token: str
    chat_id: str

@router.post("/telegram")
async def save_telegram_config(config: TelegramConfig):
    conn = sqlite3.connect('market_data.db')
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
    conn = sqlite3.connect('market_data.db')
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
    conn = sqlite3.connect('market_data.db', timeout=5.0)
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
    conn = sqlite3.connect('market_data.db', timeout=5.0)
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
    conn = sqlite3.connect('market_data.db', timeout=5.0)
    cur = conn.execute("SELECT value FROM app_settings WHERE key = 'portfolio_max_heat_cap'")
    row = cur.fetchone()
    conn.close()
    return {"max_heat_cap_pct": float(row[0]) if row else 6.0}

@router.post("/portfolio-heat")
def save_heat_cap_setting(config: HeatCapConfig):
    conn = sqlite3.connect('market_data.db', timeout=5.0)
    conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('portfolio_max_heat_cap', ?)", (str(config.max_heat_cap_pct),))
    conn.commit()
    conn.close()
    return {"status": "success", "max_heat_cap_pct": config.max_heat_cap_pct}

