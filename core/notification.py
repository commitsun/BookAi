# =====================================================
# 📣 core/notification.py
# Sistema de notificación al encargado (Telegram)
# =====================================================

import logging
import os
import asyncio
import requests
from channels_wrapper.utils.text_utils import send_fragmented_async

# =====================================================
# 🔧 Configuración desde variables de entorno
# =====================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # ID del encargado principal

log = logging.getLogger("notification")


# =====================================================
# 📤 Envío simple (con fragmentación incluida)
# =====================================================
async def _send_single(chat_id: str, text: str):
    """Envía un solo fragmento de texto al encargado."""
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        log.error("❌ Falta TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID.")
        return False

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}

        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            log.info(f"📤 Telegram → {chat_id}: {text[:60]}...")
            return True
        else:
            log.warning(f"⚠️ Error enviando Telegram (HTTP {r.status_code}): {r.text}")
            return False

    except Exception as e:
        log.error(f"💥 Error crítico enviando mensaje Telegram: {e}", exc_info=True)
        return False


# =====================================================
# 📣 Notificador principal (fragmentación + reintentos)
# =====================================================
async def notify_encargado(message: str, retries: int = 2, delay: float = 1.0) -> bool:
    """
    Envía un mensaje (fragmentado si es largo) al encargado por Telegram.

    - Usa TELEGRAM_BOT_TOKEN y TELEGRAM_ENCARGADO_CHAT_ID del .env.
    - Aplica fragmentación natural de texto.
    - Reintenta en caso de error temporal.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("❌ Faltan credenciales Telegram.")
        return False

    try:
        sent_ok = False
        for attempt in range(retries):
            try:
                # Utiliza fragmentación con delays realistas
                await send_fragmented_async(_send_single, TELEGRAM_CHAT_ID, message)
                sent_ok = True
                break
            except Exception as e:
                log.error(f"⚠️ Intento {attempt+1}/{retries} fallido: {e}")
                await asyncio.sleep(delay * (attempt + 1))
        return sent_ok

    except Exception as e:
        log.error(f"💥 Error global en notify_encargado: {e}", exc_info=True)
        return False


# =====================================================
# 👥 Notificación a múltiples encargados
# =====================================================
async def notify_multiple_encargados(message: str, chat_ids: list[str]):
    """
    Envía el mismo mensaje a varios encargados (con fragmentación y delays).
    No interrumpe el envío si alguno falla.
    """
    if not TELEGRAM_BOT_TOKEN:
        log.error("❌ Falta TELEGRAM_BOT_TOKEN.")
        return

    for cid in chat_ids:
        try:
            await send_fragmented_async(_send_single, cid, message)
            await asyncio.sleep(0.5)  # pequeño delay entre envíos
        except Exception as e:
            log.error(f"⚠️ Error notificando a {cid}: {e}", exc_info=True)
