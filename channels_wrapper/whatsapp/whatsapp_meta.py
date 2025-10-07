import os
import json
import time
import random
import requests
import asyncio
from io import BytesIO
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from channels_wrapper.base_channel import BaseChannel


# --- Configuración ---
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


class WhatsAppChannel(BaseChannel):
    def __init__(self):
        super().__init__(openai_api_key=OPENAI_API_KEY)
        self.fastapi_app = FastAPI()
        self.register_routes()

    # ------------------------------------------------------------------
    # --- Métodos específicos de WhatsApp ---
    # ------------------------------------------------------------------
    def send_message(self, user_id: str, text: str):
        url = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_ID}/messages"
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
        fragments = self.fragment_text_intelligently(text)

        for i, frag in enumerate(fragments):
            requests.post(url, headers=headers, json={"messaging_product": "whatsapp", "to": user_id, "type": "typing_on"})
            time.sleep(random.uniform(1.5, 3.5))
            r = requests.post(url, headers=headers, json={
                "messaging_product": "whatsapp",
                "to": user_id,
                "type": "text",
                "text": {"body": frag}
            })
            print(f"📤 Enviado fragmento {i+1}/{len(fragments)} ({r.status_code})")
            requests.post(url, headers=headers, json={"messaging_product": "whatsapp", "to": user_id, "type": "typing_off"})
            time.sleep(random.uniform(0.5, 1.5))

    def extract_message_data(self, payload: dict):
        """Extrae los datos relevantes de un mensaje de WhatsApp."""
        entry = payload.get("entry", [])[0]["changes"][0]["value"]
        if "messages" not in entry:
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
            user_msg = self.transcribir_audio(media_id)
        else:
            user_msg = f"[Mensaje tipo {msg_type} no soportado]"

        return user_id, msg_id, msg_type, user_msg

    # ------------------------------------------------------------------
    # --- Webhooks ---
    # ------------------------------------------------------------------
    def register_routes(self):
        @self.fastapi_app.get("/webhook")
        async def verify_webhook(request: Request):
            params = request.query_params
            if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == VERIFY_TOKEN:
                return PlainTextResponse(params.get("hub.challenge"), status_code=200)
            return PlainTextResponse("Error de verificación", status_code=403)

        @self.fastapi_app.post("/webhook")
        async def webhook(request: Request):
            data = await request.json()
            print("📩 Payload recibido:\n", json.dumps(data, indent=2, ensure_ascii=False))
            asyncio.create_task(self.process_message_async(data))
            return JSONResponse({"status": "ok"})

    # ------------------------------------------------------------------
    # --- Funciones auxiliares ---
    # ------------------------------------------------------------------
    def download_media_bytes(self, media_id: str):
        url = f"https://graph.facebook.com/v19.0/{media_id}"
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
        r = requests.get(url, headers=headers)
        if r.status_code != 200:
            return None
        media_url = r.json().get("url")
        if not media_url:
            return None
        r = requests.get(media_url, headers=headers)
        if r.status_code == 200:
            return BytesIO(r.content)
        return None

    def transcribir_audio(self, media_id: str) -> str:
        audio_bytes = self.download_media_bytes(media_id)
        if not audio_bytes:
            return "[Error: no se pudo descargar el audio]"
        transcript = self.client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_bytes,
            prompt="Pregunta de un cliente sobre un hotel."
        )
        return transcript.text.strip() or "[Audio vacío]"


# Instancia lista para usar en FastAPI
whatsapp_channel = WhatsAppChannel()
app = whatsapp_channel.fastapi_app
