# core/notification.py
import os
import logging
import time
import requests
from typing import Optional, List

# ===============================================
# CONFIGURACIÓN GLOBAL
# ===============================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # ID o canal del encargado
MAX_RETRIES = 3
RETRY_DELAY = 2.5
MAX_MESSAGE_LENGTH = 3900  # Límite seguro (Telegram máximo 4096)

# ===============================================
# ENVÍO DE MENSAJES
# ===============================================

def _split_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> List[str]:
    """
    Divide el texto largo en fragmentos seguros para Telegram.
    """
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    parts = []
    while len(text) > limit:
        split_index = text.rfind("\n", 0, limit)
        if split_index == -1:
            split_index = limit
        parts.append(text[:split_index].strip())
        text = text[split_index:].strip()
    if text:
        parts.append(text)
    return parts


def _sanitize_markdown(text: str) -> str:
    """
    Limpia caracteres conflictivos de Markdown para evitar errores en Telegram.
    """
    if not text:
        return text
    bad_chars = ["*", "_", "[", "]", "(", ")", "~", "`", ">", "#", "+", "-", "=", "|", "{", "}", ".", "!"]
    clean = text
    for c in bad_chars:
        clean = clean.replace(c, f"\\{c}")
    return clean


def notify_encargado(message: str, parse_mode: str = "Markdown") -> bool:
    """
    Envía un mensaje al encargado (Telegram) con manejo de errores, reintentos y fragmentación.
    Retorna True si al menos un mensaje se entrega correctamente.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("❌ Falta TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    fragments = _split_message(message)
    success = False

    for i, fragment in enumerate(fragments):
        sanitized = _sanitize_markdown(fragment)
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": sanitized,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                r = requests.post(url, json=payload, timeout=10)
                if r.status_code == 200:
                    logging.info(f"📨 Telegram → Encargado ({len(fragment)} chars) OK (parte {i+1}/{len(fragments)})")
                    success = True
                    break
                else:
                    logging.warning(
                        f"⚠️ Telegram falló HTTP {r.status_code} (intento {attempt}/{MAX_RETRIES}): {r.text}"
                    )
            except Exception as e:
                logging.error(f"💥 Error enviando a Telegram (intento {attempt}): {e}", exc_info=True)

            time.sleep(RETRY_DELAY)

    return success
