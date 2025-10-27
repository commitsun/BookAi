# =====================================================
# 📣 core/notification.py
# Sistema de notificación al encargado (Telegram)
# =====================================================

import logging
import os
import requests

# =====================================================
# 🔧 Configuración desde variables de entorno (exactas según tu .env)
# =====================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_ENCARGADO_CHAT_ID")  # 👈 nombre adaptado al .env actual

# =====================================================
# 📤 Notificador principal
# =====================================================
async def notify_encargado(message: str) -> bool:
    """
    Envía un mensaje al encargado del hotel vía Telegram.

    - Usa TELEGRAM_BOT_TOKEN y TELEGRAM_ENCARGADO_CHAT_ID del .env.
    - Devuelve True si se envía correctamente, False en caso contrario.
    - Es totalmente compatible con `await`.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("❌ Falta TELEGRAM_BOT_TOKEN o TELEGRAM_ENCARGADO_CHAT_ID.")
        return False

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
        }

        response = requests.post(url, json=payload, timeout=10)
        status = response.status_code

        if status == 200:
            logging.info(f"📤 Notificación enviada correctamente al encargado (HTTP {status})")
            return True
        else:
            logging.warning(f"⚠️ Error enviando notificación (HTTP {status}): {response.text}")
            return False

    except Exception as e:
        logging.error(f"💥 Error crítico enviando mensaje a Telegram: {e}", exc_info=True)
        return False


# =====================================================
# 🧩 Envío a múltiples encargados (opcional)
# =====================================================
async def notify_multiple_encargados(message: str, chat_ids: list[str]) -> None:
    """
    Envía el mismo mensaje a varios encargados de soporte (opcional).

    - Usa el mismo bot definido por TELEGRAM_BOT_TOKEN.
    - No interrumpe si alguno falla.
    """
    if not TELEGRAM_BOT_TOKEN:
        logging.error("❌ Falta TELEGRAM_BOT_TOKEN.")
        return

    for cid in chat_ids:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": cid,
                "text": message,
                "parse_mode": "Markdown",
            }
            r = requests.post(url, json=payload, timeout=10)
            logging.info(f"📨 Notificación enviada a {cid} (HTTP {r.status_code})")
        except Exception as e:
            logging.error(f"⚠️ Error notificando a {cid}: {e}", exc_info=True)
