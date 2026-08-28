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
