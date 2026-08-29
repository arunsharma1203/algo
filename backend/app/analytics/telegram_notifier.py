import os
import sqlite3
import requests
import logging

logger = logging.getLogger(__name__)

def get_db_path() -> str:
    """Resolves authoritative market_data.db file path."""
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    backend_db = os.path.join(base_dir, "market_data.db")
    if os.path.exists(backend_db):
        return backend_db
    root_db = os.path.abspath(os.path.join(base_dir, "..", "market_data.db"))
    if os.path.exists(root_db):
        return root_db
    return backend_db

def send_telegram_message(message: str) -> bool:
    """
    Pushes an actionable trade or risk notification to the configured Telegram chat.
    Safe failure tolerance: returns False if credentials are not configured or network fails.
    """
    try:
        db_path = get_db_path()
        conn = sqlite3.connect(db_path, timeout=5.0)
        cur = conn.cursor()
        cur.execute("SELECT value FROM app_settings WHERE key = 'telegram_bot_token'")
        token = cur.fetchone()
        cur.execute("SELECT value FROM app_settings WHERE key = 'telegram_chat_id'")
        chat = cur.fetchone()
        conn.close()
        
        if not token or not chat or not str(token[0]).strip() or not str(chat[0]).strip():
            logger.info("Telegram not configured. Skipping push notification.")
            return False
            
        bot_token = str(token[0]).strip()
        chat_id = str(chat[0]).strip()
        
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML"
        }
        
        response = requests.post(url, json=payload, timeout=5)
        if response.status_code == 200:
            logger.info("Telegram push notification sent successfully.")
            return True
        else:
            logger.error(f"Telegram API Error: {response.text}")
            return False
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")
        return False
