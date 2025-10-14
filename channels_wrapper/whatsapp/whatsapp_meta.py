# channels_wrapper/whatsapp/whatsapp_meta.py
import os
import json
import asyncio
import logging
import requests
from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse

from channels_wrapper.base_channel import BaseChannel
from channels_wrapper.utils.media_utils import transcribe_audio

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


class WhatsAppChannel(BaseChannel):
    """Canal WhatsApp (Meta Graph API) — con buffer 10s + interrupción."""

    def __init__(self, openai_api_key: str = None):
        super().__init__(openai_api_key=openai_api_key or OPENAI_API_KEY)

    # =====================================================
    # Rutas FastAPI
    # =====================================================
    def register_routes(self, app):
        @app.get("/webhook")
        @app.get("/webhook/whatsapp")
        async def verify_webhook(request: Request):
            params = request.query_params
            if (
                params.get("hub.mode") == "subscribe"
                and params.get("hub.verify_token") == VERIFY_TOKEN
            ):
                logging.info("✅ Webhook WhatsApp verificado.")
                return PlainTextResponse(params.get("hub.challenge"), status_code=200)
            return PlainTextResponse("Error de verificación", status_code=403)

        @app.post("/webhook")
        @app.post("/webhook/whatsapp")
        async def whatsapp_webhook(request: Request):
            logging.info("⚡️ [Webhook] POST recibido desde WhatsApp")
            try:
                data = await request.json()
                # Procesamos de forma asíncrona para responder 200 OK al webhook cuanto antes
                asyncio.create_task(self._handle_event(data))
                return JSONResponse({"status": "ok"})
            except Exception as e:
                logging.error(f"❌ Error procesando webhook: {e}", exc_info=True)
                return JSONResponse({"status": "error", "detail": str(e)})

    # =====================================================
    # Entrada por webhook → Buffer manager
    # =====================================================
    async def _handle_event(self, data: dict):
        try:
            await self.process_message_async(data)
        except Exception as e:
            logging.error(f"💥 Error manejando evento WhatsApp: {e}", exc_info=True)

    # =====================================================
    # Envío de mensajes
    # =====================================================
    def send_message(self, user_id: str, text: str):
        """Envía mensaje a WhatsApp."""
        url = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_ID}/messages"
        headers = {
            "Authorization": f"Bearer {WHATSAPP_TOKEN}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": user_id,
            "type": "text",
            "text": {"body": text},
        }
        logging.info(f"🚀 WhatsApp → {user_id}: {text[:120]}...")
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=10)
            logging.debug(f"📬 META RESPUESTA ({r.status_code}): {r.text}")
        except Exception as e:
            logging.error(f"⚠️ Error enviando mensaje WhatsApp: {e}", exc_info=True)

    # =====================================================
    # Parsing del payload de Meta (robusto)
    # =====================================================
    def extract_message_data(self, payload: dict):
        """Devuelve (user_id, msg_id, msg_type, user_msg)."""
        try:
            entries = payload.get("entry", [])
            if not entries:
                logging.debug("⚠️ Payload sin 'entry'.")
                return None, None, None, None

            changes = entries[0].get("changes", [])
            if not changes:
                logging.debug("⚠️ Payload sin 'changes'.")
                return None, None, None, None

            value = changes[0].get("value", {})
            messages = value.get("messages", [])

            # Si no hay 'messages', puede ser un 'status update'
            if not messages:
                statuses = value.get("statuses", [])
                if statuses:
                    logging.info("ℹ️ Webhook de estado (entregas/lecturas).")
                else:
                    logging.debug("⚠️ Webhook sin mensajes ni estados.")
                return None, None, None, None

            msg = messages[0]
            msg_type = msg.get("type")
            user_id = msg.get("from")
            msg_id = msg.get("id")

            # Extraer contenido según tipo
            if msg_type == "text":
                user_msg = msg.get("text", {}).get("body", "").strip()

            elif msg_type == "interactive":
                interactive = msg.get("interactive", {})
                if "button_reply" in interactive:
                    user_msg = interactive["button_reply"].get("title", "")
                elif "list_reply" in interactive:
                    user_msg = interactive["list_reply"].get("title", "")
                else:
                    user_msg = "[Respuesta interactiva]"

            elif msg_type == "audio":
                media_id = msg.get("audio", {}).get("id")
                user_msg = transcribe_audio(media_id, WHATSAPP_TOKEN, OPENAI_API_KEY)

            elif msg_type == "image":
                user_msg = msg.get("image", {}).get("caption", "Imagen recibida.")

            else:
                user_msg = f"[Tipo de mensaje no soportado: {msg_type}]"

            logging.info(f"💬 WhatsApp → {user_id} [{msg_type}]: {user_msg}")
            return user_id, msg_id, msg_type, (user_msg or None)

        except Exception as e:
            logging.error(f"⚠️ Error extrayendo datos del mensaje: {e}", exc_info=True)
            return None, None, None, None
