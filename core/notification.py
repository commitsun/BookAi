# core/notification.py
import aiohttp
import logging
import os

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_ENCARGADO_CHAT_ID = os.getenv("TELEGRAM_ENCARGADO_CHAT_ID")


async def notify_encargado(mensaje: str):
    """Envía un mensaje al encargado del hotel vía Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_ENCARGADO_CHAT_ID:
        logging.warning("⚠️ Variables de Telegram no configuradas.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_ENCARGADO_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=data, timeout=15) as resp:
                logging.info(f"📨 Aviso al encargado (HTTP {resp.status})")
    except Exception as e:
        logging.error(f"❌ Error enviando aviso a Telegram: {e}", exc_info=True)
