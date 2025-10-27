import logging
import os
import requests
from fastapi import Request
from fastapi.responses import JSONResponse
from channels_wrapper.base_channel import BaseChannel
from core.escalation_manager import resolve_from_encargado, pending_escalations
from core.notification import notify_encargado

# =====================================================
# 🔑 TOKEN DEL BOT DE TELEGRAM
# =====================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


class TelegramChannel(BaseChannel):
    """Canal Telegram: recibe respuestas del encargado y las reenvía al huésped."""

    # =====================================================
    # Métodos requeridos por BaseChannel
    # =====================================================
    def send_message(self, user_id: str, text: str):
        """
        Envía un mensaje al chat de Telegram del encargado o huésped.
        """
        if not TELEGRAM_BOT_TOKEN or not user_id:
            logging.error("❌ Falta TELEGRAM_BOT_TOKEN o user_id.")
            return

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {"chat_id": user_id, "text": text, "parse_mode": "Markdown"}

        try:
            r = requests.post(url, json=data, timeout=10)
            if r.status_code != 200:
                logging.error(f"⚠️ Telegram API error ({r.status_code}): {r.text}")
            else:
                logging.info(f"📤 Telegram → {user_id} (HTTP {r.status_code})")
        except Exception as e:
            logging.error(f"⚠️ Error enviando mensaje Telegram: {e}", exc_info=True)

    def extract_message_data(self, payload: dict):
        """
        Implementación vacía (no se usa en Telegram).
        """
        return None, None, None, None

    # =====================================================
    # Registro del webhook
    # =====================================================
    def register_routes(self, app):
        """
        Registra el endpoint /telegram/webhook en FastAPI.
        """
        @app.post("/telegram/webhook")
        async def telegram_webhook(request: Request):
            """
            Webhook principal de Telegram: recibe mensajes del encargado y los gestiona.
            """
            try:
                data = await request.json()
                message = data.get("message", {})
                chat_id = message.get("chat", {}).get("id")
                text = message.get("text", "")

                if not text:
                    return JSONResponse({"ok": True})

                logging.info(f"💬 Telegram → Encargado [{chat_id}]: {text}")

                # =====================================================
                # Caso 1️⃣: El encargado usa formato "RESPUESTA <id>: <mensaje>"
                # =====================================================
                if text.lower().startswith("respuesta "):
                    try:
                        content = text.split(" ", 1)[1]
                        target_id, respuesta = content.split(":", 1)
                        target_id = target_id.strip()
                        respuesta = respuesta.strip()

                        from main import hybrid_agent
                        await resolve_from_encargado(target_id, respuesta, hybrid_agent)
                        await notify_encargado(f"✅ Respuesta enviada al cliente {target_id}.")
                    except Exception as e:
                        logging.error(f"❌ Error procesando RESPUESTA: {e}", exc_info=True)
                        await notify_encargado("⚠️ Formato incorrecto. Usa RESPUESTA <id>: <mensaje>.")
                    return JSONResponse({"ok": True})

                # =====================================================
                # Caso 2️⃣: Respuesta directa (solo hay una conversación pendiente)
                # =====================================================
                if len(pending_escalations) == 1:
                    target_id = next(iter(pending_escalations.keys()))
                    respuesta = text.strip()
                    logging.info(f"✉️ Respuesta directa del encargado → {target_id}: {respuesta}")

                    from main import hybrid_agent
                    await resolve_from_encargado(target_id, respuesta, hybrid_agent)
                    await notify_encargado(f"✅ Respuesta enviada automáticamente al cliente {target_id}.")

                # =====================================================
                # Caso 3️⃣: Hay varias conversaciones pendientes
                # =====================================================
                elif len(pending_escalations) > 1:
                    ids = "\n".join([f"• {cid}" for cid in pending_escalations.keys()])
                    await notify_encargado(
                        f"⚠️ Hay varias conversaciones pendientes.\n"
                        f"Usa el formato:\n\nRESPUESTA <id>: <mensaje>\n\n"
                        f"Clientes pendientes:\n{ids}"
                    )

                # =====================================================
                # Caso 4️⃣: No hay ninguna conversación pendiente
                # =====================================================
                else:
                    await notify_encargado("⚠️ No hay conversaciones pendientes en este momento.")

                return JSONResponse({"ok": True})

            except Exception as e:
                logging.error(f"❌ Error procesando webhook de Telegram: {e}", exc_info=True)
                return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
