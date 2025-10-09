import os
import json
import asyncio
import requests
import logging
from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse
from channels_wrapper.base_channel import BaseChannel
from channels_wrapper.utils.media_utils import transcribe_audio

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


class WhatsAppChannel(BaseChannel):
    def __init__(self, openai_api_key: str = None):
        super().__init__(openai_api_key=openai_api_key or os.getenv("OPENAI_API_KEY"))

    def register_routes(self, app):
        @app.get("/webhook")
        @app.get("/webhook/whatsapp")
        async def verify_webhook(request: Request):
            params = request.query_params
            mode = params.get("hub.mode")
            token = params.get("hub.verify_token")
            challenge = params.get("hub.challenge")
            if mode == "subscribe" and token == VERIFY_TOKEN:
                logging.info("✅ Webhook de WhatsApp verificado.")
                return PlainTextResponse(challenge, status_code=200)
            logging.warning("❌ Error de verificación de WhatsApp.")
            return PlainTextResponse("Error de verificación", status_code=403)

        @app.post("/webhook")
        @app.post("/webhook/whatsapp")
        async def whatsapp_webhook(request: Request):
            logging.info("⚡️ [Webhook] POST desde WhatsApp")
            try:
                data = await request.json()
                logging.debug("📩 PAYLOAD:\n" + json.dumps(data, indent=2, ensure_ascii=False))
            except Exception as e:
                logging.error(f"❌ ERROR payload: {e}")
                return JSONResponse({"status": "error", "detail": str(e)})
            asyncio.create_task(self._process_in_background(data))
            return JSONResponse({"status": "ok"})

    async def _process_in_background(self, data: dict):
        try:
            user_id, msg_id, msg_type, user_message = self.extract_message_data(data)
            if not user_id or not user_message:
                logging.warning("⚠️ Mensaje inválido o sin texto, se ignora.")
                return

            # Ejecutar agente
            from main import hybrid_agent, mark_pending  # hooks globales
            response = await hybrid_agent.process_message(user_message, user_id)

            # ¿Escalación en pausa?
            if any(p in response.lower() for p in [
                "contactar con el encargado",
                "no dispongo",
                "permíteme contactar",
                "he contactado con el encargado",
                "error",
            ]):
                # NO enviamos respuesta al huésped: marcamos pendiente y avisamos al encargado
                await mark_pending(user_id, user_message)
                logging.info(f"🕓 Conversación {user_id} marcada como pendiente (esperando encargado).")
                return

            # Respuesta normal → enviar al huésped
            self.send_message(user_id, response)

        except Exception as e:
            logging.error(f"💥 ERROR en background WhatsApp: {e}", exc_info=True)

    def send_message(self, user_id: str, text: str):
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
        logging.info(f"🚀 ENVIANDO A {user_id}: {text[:120]}...")
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=10)
            logging.debug(f"📬 META ({r.status_code}): {r.text}")
        except Exception as e:
            logging.error(f"⚠️ ERROR enviando WhatsApp: {e}", exc_info=True)

    def extract_message_data(self, payload: dict):
        try:
            entry = payload.get("entry", [])[0].get("changes", [])[0].get("value", {})
            if not entry or "messages" not in entry:
                return None, None, None, None
            msg = entry["messages"][0]
            msg_type = msg.get("type")
            user_id = msg.get("from")
            msg_id = msg.get("id")
            user_msg = None

            if msg_type == "text":
                user_msg = msg.get("text", {}).get("body", "").strip()
            elif msg_type == "image":
                user_msg = msg.get("image", {}).get("caption", "El cliente envió una imagen.")
            elif msg_type == "audio":
                media_id = msg.get("audio", {}).get("id")
                try:
                    user_msg = transcribe_audio(media_id, WHATSAPP_TOKEN, OPENAI_API_KEY)
                except Exception as e:
                    logging.error(f"Error transcribiendo audio: {e}", exc_info=True)
                    user_msg = "[Audio recibido, pero no se pudo transcribir]"
            elif msg_type == "interactive":
                i = msg.get("interactive", {})
                user_msg = i.get("button_reply", {}).get("title") or i.get("list_reply", {}).get("title") or "[Interacción recibida]"
            else:
                user_msg = f"[Mensaje tipo {msg_type} no soportado]"

            logging.info(f"💬 WhatsApp → {user_id}: {user_msg}")
            return user_id, msg_id, msg_type, user_msg

        except Exception as e:
            logging.error(f"⚠️ ERROR extrayendo datos del mensaje: {e}", exc_info=True)
            return None, None, None, None
