import logging
import os
import requests
from fastapi import Request
from fastapi.responses import JSONResponse

from channels_wrapper.base_channel import BaseChannel
from channels_wrapper.manager import ChannelManager
from agents.interno_agent import InternoAgent
from tools.interno_tool import ESCALATIONS_STORE  # ✅ nueva ubicación

log = logging.getLogger("telegram")

# ============================================================
# 🔧 Configuración inicial
# ============================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    log.warning("⚠️ TELEGRAM_BOT_TOKEN no está configurado en el entorno.")

# Instancia global del agente interno
interno_agent = InternoAgent()

# Almacén temporal para rastrear confirmaciones (chat_id → escalation_id)
TELEGRAM_REPLY_TRACKER = {}


# ============================================================
# 🚀 Canal Telegram - Comunicación con encargado
# ============================================================
class TelegramChannel(BaseChannel):
    """Canal Telegram: encargado ↔ huésped (gestión de escalaciones y confirmaciones)."""

    # ----------------------------------------------------------
    # 🔹 Implementación requerida por BaseChannel
    # ----------------------------------------------------------
    def extract_message_data(self, payload):
        """
        Extrae los datos clave de un mensaje entrante de Telegram.
        Cumple con la interfaz de BaseChannel.
        Devuelve: (user_id, message_id, message_type, message_text)
        """
        try:
            message = payload.get("message", {})
            chat = message.get("chat", {})
            user_id = str(chat.get("id", "")) or None
            message_id = str(message.get("message_id", "")) or None
            message_type = "text"
            message_text = (message.get("text") or "").strip() or None
            return user_id, message_id, message_type, message_text
        except Exception as e:
            log.error(f"⚠️ Error extrayendo datos de mensaje Telegram: {e}", exc_info=True)
            return None, None, None, None

    # ----------------------------------------------------------
    # 🔹 Envío de mensajes
    # ----------------------------------------------------------
    def send_message(self, user_id: str, text: str):
        """Envía mensaje al encargado (modo clásico)."""
        if not TELEGRAM_BOT_TOKEN or not user_id:
            log.error("❌ Falta TELEGRAM_BOT_TOKEN o user_id.")
            return

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {
            "chat_id": str(user_id),
            "text": text,
        }

        try:
            r = requests.post(url, json=data, timeout=10)
            if r.status_code != 200:
                log.error(f"⚠️ Telegram API error ({r.status_code}): {r.text}")
            else:
                log.info(f"📤 Telegram → {user_id}: {text[:60]}...")
        except Exception as e:
            log.error(f"💥 Error enviando Telegram: {e}", exc_info=True)

    # ----------------------------------------------------------
    # 🔹 Registro de rutas (webhook)
    # ----------------------------------------------------------
    def register_routes(self, app):
        @app.post("/telegram/webhook")
        async def telegram_webhook(request: Request):
            """
            Webhook para manejar las respuestas y confirmaciones del encargado.
            - Si responde a una escalación (reply): genera borrador.
            - Si responde “OK” o texto nuevo: confirma o ajusta.
            """
            try:
                data = await request.json()
                message = data.get("message", {})
                chat = message.get("chat", {})
                chat_id = str(chat.get("id"))
                text = (message.get("text") or "").strip()
                reply_to = message.get("reply_to_message")

                if not text:
                    return JSONResponse({"ok": True})

                log.info(f"💬 Telegram ({chat_id}): {text}")

                # =========================================================
                # Caso 1: Encargado responde a un mensaje de escalación
                # =========================================================
                if reply_to:
                    original_text = reply_to.get("text", "") or ""
                    escalation_id = None

                    # 🔧 Limpieza de markdown para detección robusta
                    clean_original = (
                        original_text.replace("`", "")
                        .replace("*", "")
                        .replace("_", "")
                        .replace("~", "")
                    )

                    for eid in ESCALATIONS_STORE.keys():
                        if eid in clean_original:
                            escalation_id = eid
                            break

                    if not escalation_id:
                        log.warning("⚠️ No se pudo determinar la escalación asociada al reply.")
                        return JSONResponse({"ok": False, "error": "No escalation_id found"})

                    TELEGRAM_REPLY_TRACKER[chat_id] = escalation_id

                    # 🧠 Generar borrador desde la respuesta del encargado
                    draft = await interno_agent.process_manager_reply(escalation_id, text)

                    # Enviar borrador al encargado para su revisión
                    channel_manager = ChannelManager()
                    await channel_manager.send_message(
                        chat_id=str(chat_id),
                        message=(
                            f"📝 *Borrador generado para {escalation_id}:*\n\n"
                            f"{draft}\n\n"
                            "Confirma con 'OK' o ajusta el texto para enviar al huésped."
                        ),
                        channel="telegram",
                    )

                    return JSONResponse({"ok": True, "status": "draft_generated"})

                # =========================================================
                # Caso 2: Confirmación o ajuste del borrador
                # =========================================================
                if chat_id in TELEGRAM_REPLY_TRACKER:
                    escalation_id = TELEGRAM_REPLY_TRACKER[chat_id]

                    if text.lower() == "ok":
                        resp = await interno_agent.send_confirmed_response(escalation_id, confirmed=True)
                    else:
                        resp = await interno_agent.send_confirmed_response(
                            escalation_id, confirmed=True, adjustments=text
                        )

                    channel_manager = ChannelManager()
                    await channel_manager.send_message(
                        chat_id=str(chat_id),
                        message=f"✅ {resp}",
                        channel="telegram",
                    )

                    TELEGRAM_REPLY_TRACKER.pop(chat_id, None)
                    return JSONResponse({"ok": True, "status": "confirmed"})

                # =========================================================
                # Caso 3: Mensaje sin contexto de escalación
                # =========================================================
                log.info("ℹ️ Mensaje ignorado (sin escalación activa).")
                channel_manager = ChannelManager()
                await channel_manager.send_message(
                    chat_id=str(chat_id),
                    message="ℹ️ No hay ninguna escalación activa vinculada a este chat.",
                    channel="telegram",
                )
                return JSONResponse({"ok": True, "status": "ignored"})

            except Exception as e:
                log.error(f"💥 Error en Telegram webhook: {e}", exc_info=True)
                return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
