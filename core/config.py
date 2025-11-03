# core/config.py
from dotenv import load_dotenv
import os

load_dotenv()

class Settings:
    """Configuración centralizada del proyecto HOTEL_AI"""

    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

    # WhatsApp / Meta
    WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
    WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")
    WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")

    # Telegram / encargado
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

    # MCP / Supabase
    ENDPOINT_MCP = os.getenv("ENDPOINT_MCP")
    SUPABASE_URL = os.getenv("SUPABASE_URL")
    SUPABASE_KEY = os.getenv("SUPABASE_KEY")
