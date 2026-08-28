import sqlite3
import requests
import logging

logger = logging.getLogger(__name__)

def send_telegram_message(message: str):
    try:
        conn = sqlite3.connect('market_data.db')
        cur = conn.cursor()
        cur.execute("SELECT value FROM app_settings WHERE key = 'telegram_bot_token'")
        token = cur.fetchone()
        cur.execute("SELECT value FROM app_settings WHERE key = 'telegram_chat_id'")
        chat = cur.fetchone()
        conn.close()
        
        if not token or not chat or not token[0] or not chat[0]:
            logger.info("Telegram not configured. Skipping push notification.")
            return False
            
        bot_token = token[0]
        chat_id = chat[0]
        
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
