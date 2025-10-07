import os
import json
import asyncio
import requests
from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse
from channels_wrapper.base_channel import BaseChannel
from channels_wrapper.utils.media_utils import transcribe_audio

# --- Configuración de entorno ---
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


class WhatsAppChannel(BaseChannel):
    """Canal específico para la integración con WhatsApp (Meta Graph API)."""

    def __init__(self):
        super().__init__(openai_api_key=OPENAI_API_KEY)

    # ------------------------------------------------------------------
    # 📡 Rutas FastAPI
    # ------------------------------------------------------------------
    def register_routes(self, app):
        """Registra endpoints GET y POST del webhook."""
        @app.get("/webhook/whatsapp")
        @app.get("/webhook")  # alias por compatibilidad con Meta
        async def verify_webhook(request: Request):
            params = request.query_params
            mode = params.get("hub.mode")
            token = params.get("hub.verify_token")
            challenge = params.get("hub.challenge")

            if mode == "subscribe" and token == VERIFY_TOKEN:
                print("✅ Webhook de WhatsApp verificado correctamente.")
                return PlainTextResponse(challenge, status_code=200)

            print("❌ Error de verificación de WhatsApp.")
            return PlainTextResponse("Error de verificación", status_code=403)

        @app.post("/webhook/whatsapp")
        @app.post("/webhook")  # alias por compatibilidad con Meta
        async def whatsapp_webhook(request: Request):
            data = await request.json()
            print("📩 Payload WhatsApp recibido:\n", json.dumps(data, indent=2, ensure_ascii=False))
            asyncio.create_task(self.process_message_async(data))
            return JSONResponse({"status": "ok"})

    # ------------------------------------------------------------------
    # 💬 Envío de mensajes
    # ------------------------------------------------------------------
    def send_message(self, user_id: str, text: str):
        """Envía un mensaje al usuario a través de la API de WhatsApp."""
        url = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_ID}/messages"
        headers = {
            "Authorization": f"Bearer {WHATSAPP_TOKEN}",
            "Content-Type": "application/json"
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": user_id,
            "type": "text",
            "text": {"body": text},
        }
        try:
            r = requests.post(url, headers=headers, json=payload)
            print(f"📤 WhatsApp → {user_id} ({r.status_code})")
        except Exception as e:
            print(f"⚠️ Error enviando mensaje a WhatsApp: {e}")

    # ------------------------------------------------------------------
    # 📥 Extracción de datos del payload
    # ------------------------------------------------------------------
    def extract_message_data(self, payload: dict):
        """
        Extrae datos útiles del payload entrante:
        - user_id
        - msg_id
        - tipo
        - texto del usuario o transcripción
        """
        try:
            entry = payload.get("entry", [])[0]["changes"][0]["value"]
            if "messages" not in entry:
                print("ℹ️ No hay mensajes en el payload (evento de estado).")
                return None, None, None, None

            msg = entry["messages"][0]
            msg_type = msg.get("type")
            user_id = msg["from"]
            msg_id = msg.get("id")

            if msg_type == "text":
                user_msg = msg["text"]["body"]
            elif msg_type == "image":
                user_msg = msg["image"].get("caption", "El cliente envió una imagen.")
            elif msg_type == "audio":
                media_id = msg["audio"]["id"]
                user_msg = transcribe_audio(media_id, WHATSAPP_TOKEN, OPENAI_API_KEY)
            else:
                user_msg = f"[Mensaje tipo {msg_type} no soportado]"

            return user_id, msg_id, msg_type, user_msg

        except Exception as e:
            print(f"⚠️ Error extrayendo datos del mensaje: {e}")
            return None, None, None, None
