import os
import json
import asyncio
import requests
from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse
from channels_wrapper.base_channel import BaseChannel
from channels_wrapper.utils.media_utils import transcribe_audio

# --- Variables de entorno ---
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


class WhatsAppChannel(BaseChannel):
    """Canal específico para la integración con WhatsApp (Meta Graph API)."""

    def __init__(self, openai_api_key: str = None):
        super().__init__(openai_api_key=openai_api_key or os.getenv("OPENAI_API_KEY"))


    # ------------------------------------------------------------------
    # 📡 Webhook de Meta (GET + POST)
    # ------------------------------------------------------------------
    def register_routes(self, app):
        """Registra endpoints GET (verificación) y POST (mensajería)."""

        # --- Verificación inicial del webhook ---
        @app.get("/webhook")
        @app.get("/webhook/whatsapp")
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

        # --- Recepción de mensajes ---
        @app.post("/webhook")
        @app.post("/webhook/whatsapp")
        async def whatsapp_webhook(request: Request):
            print("⚡️ [Webhook] LLEGÓ UN POST A /webhook")

            try:
                data = await request.json()
                print("📩 PAYLOAD COMPLETO:\n", json.dumps(data, indent=2, ensure_ascii=False))
            except Exception as e:
                print(f"❌ ERROR al leer payload: {e}")
                return JSONResponse({"status": "error", "detail": str(e)})

            # ✅ Procesar en segundo plano para no bloquear la respuesta a Meta
            asyncio.create_task(self._process_in_background(data))

            # ✅ Responder inmediatamente a Meta (importante para evitar timeout)
            return JSONResponse({"status": "ok"})

    # ------------------------------------------------------------------
    # 🧩 Procesamiento en background
    # ------------------------------------------------------------------
    async def _process_in_background(self, data: dict):
        """Ejecuta la lógica del agente sin bloquear el webhook."""
        try:
            await self.process_message_async(data)
        except Exception as e:
            print(f"💥 ERROR EN process_message_async (background): {e}")

    # ------------------------------------------------------------------
    # 💬 Envío de mensajes al usuario
    # ------------------------------------------------------------------
    def send_message(self, user_id: str, text: str):
        """Envía un mensaje de texto al usuario vía WhatsApp API."""
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

        print(f"🚀 ENVIANDO A {user_id}: {text[:80]}...")
        try:
            r = requests.post(url, headers=headers, json=payload)
            print("📬 RESPUESTA DE META:", r.status_code, r.text)
        except Exception as e:
            print(f"⚠️ ERROR ENVIANDO MENSAJE: {e}")

    # ------------------------------------------------------------------
    # 📥 Extracción de datos del mensaje
    # ------------------------------------------------------------------
    def extract_message_data(self, payload: dict):
        """
        Extrae los datos esenciales del mensaje entrante:
        - user_id
        - msg_id
        - tipo (text, image, audio)
        - texto o transcripción
        """
        try:
            entry = payload.get("entry", [])[0]["changes"][0]["value"]

            # Ignorar eventos de estado
            if "messages" not in entry:
                print("ℹ️ Evento sin 'messages' (probablemente status update).")
                return None, None, None, None

            msg = entry["messages"][0]
            msg_type = msg.get("type")
            user_id = msg["from"]
            msg_id = msg.get("id")

            # 🧠 Detectar tipo de mensaje
            if msg_type == "text":
                user_msg = msg["text"]["body"]
            elif msg_type == "image":
                user_msg = msg["image"].get("caption", "El cliente envió una imagen.")
            elif msg_type == "audio":
                media_id = msg["audio"]["id"]
                user_msg = transcribe_audio(media_id, WHATSAPP_TOKEN, OPENAI_API_KEY)
            else:
                user_msg = f"[Mensaje tipo {msg_type} no soportado]"

            print(f"🧩 EXTRACTION OK → user_id={user_id}, msg_id={msg_id}, tipo={msg_type}, texto={user_msg}")
            return user_id, msg_id, msg_type, user_msg

        except Exception as e:
            print(f"⚠️ ERROR extrayendo datos del mensaje: {e}")
            return None, None, None, None
