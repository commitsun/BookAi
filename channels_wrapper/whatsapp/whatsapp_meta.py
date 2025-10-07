import os
import json
import asyncio
import requests
from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse
from channels_wrapper.base_channel import BaseChannel

# --- Configuración ---
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


class WhatsAppChannel(BaseChannel):
    """Canal específico para integrar WhatsApp con Meta API."""

    def __init__(self):
        super().__init__(openai_api_key=OPENAI_API_KEY)

    # ------------------------------------------------------------------
    # --- Registro de endpoints ---
    # ------------------------------------------------------------------
    def register_routes(self, app):
        """Registra las rutas FastAPI del canal WhatsApp."""

        # ✅ Webhook para verificación de Meta
        @app.get("/webhook/whatsapp")
        @app.get("/webhook")  
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

        # ✅ Webhook principal (recepción de mensajes)
        @app.post("/webhook/whatsapp")
        @app.post("/webhook")  # alias para compatibilidad con Meta
        async def whatsapp_webhook(request: Request):
            data = await request.json()
            print("📩 Payload WhatsApp recibido:\n", json.dumps(data, indent=2, ensure_ascii=False))
            asyncio.create_task(self.process_message_async(data))
            return JSONResponse({"status": "ok"})

    # ------------------------------------------------------------------
    # --- Métodos requeridos por BaseChannel ---
    # ------------------------------------------------------------------
    def send_message(self, user_id: str, text: str):
        """Envía un mensaje de texto al usuario a través de la API de WhatsApp."""
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

        r = requests.post(url, headers=headers, json=payload)
        print(f"📤 Mensaje enviado a {user_id} (status {r.status_code})")

    def extract_message_data(self, payload: dict):
        """Extrae los datos necesarios del payload de WhatsApp."""
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
            else:
                user_msg = f"[Mensaje de tipo {msg_type} no soportado]"

            return user_id, msg_id, msg_type, user_msg

        except Exception as e:
            print(f"⚠️ Error extrayendo datos del mensaje: {e}")
            return None, None, None, None
