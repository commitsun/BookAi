import os
import json
import asyncio
import requests
import logging
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
                logging.info("✅ Webhook de WhatsApp verificado correctamente.")
                return PlainTextResponse(challenge, status_code=200)

            logging.warning("❌ Error de verificación de WhatsApp.")
            return PlainTextResponse("Error de verificación", status_code=403)

        # --- Recepción de mensajes ---
        @app.post("/webhook")
        @app.post("/webhook/whatsapp")
        async def whatsapp_webhook(request: Request):
            logging.info("⚡️ [Webhook] POST recibido desde WhatsApp")

            try:
                data = await request.json()
                logging.debug("📩 PAYLOAD COMPLETO:\n" + json.dumps(data, indent=2, ensure_ascii=False))
            except Exception as e:
                logging.error(f"❌ ERROR al leer payload: {e}")
                return JSONResponse({"status": "error", "detail": str(e)})

            # ✅ Procesar en segundo plano (no bloquear respuesta a Meta)
            asyncio.create_task(self._process_in_background(data))

            # ✅ Meta necesita respuesta inmediata (timeout < 5s)
            return JSONResponse({"status": "ok"})

    # ------------------------------------------------------------------
    # 🧩 Procesamiento en background
    # ------------------------------------------------------------------
    async def _process_in_background(self, data: dict):
        """Ejecuta la lógica del agente sin bloquear el webhook."""
        try:
            await self.process_message_async(data)
        except Exception as e:
            logging.error(f"💥 ERROR EN process_message_async (background): {e}", exc_info=True)

    # ------------------------------------------------------------------
    # 💬 Envío de mensajes al usuario
    # ------------------------------------------------------------------
    def send_message(self, user_id: str, text: str):
        """Envía un mensaje de texto al usuario vía WhatsApp API."""
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
            logging.debug(f"📬 RESPUESTA DE META ({r.status_code}): {r.text}")
        except Exception as e:
            logging.error(f"⚠️ ERROR ENVIANDO MENSAJE: {e}", exc_info=True)

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
            entry = payload.get("entry", [])[0].get("changes", [])[0].get("value", {})
            if not entry:
                logging.warning("⚠️ Payload sin 'value' válido.")
                return None, None, None, None

            # 🔇 Ignorar eventos de estado (sent/delivered/read)
            if "messages" not in entry:
                status_data = entry.get("statuses", [{}])[0]
                status = status_data.get("status")
                msg_id = status_data.get("id")
                if status:
                    logging.debug(f"📦 WhatsApp status → {status} (id={msg_id})")
                return None, None, None, None

            # 📦 Extraer datos del mensaje real
            msg = entry["messages"][0]
            msg_type = msg.get("type")
            user_id = msg.get("from")
            msg_id = msg.get("id")
            user_msg = None

            # 🔠 Texto
            if msg_type == "text":
                user_msg = msg.get("text", {}).get("body", "").strip()

            # 🖼️ Imagen con caption
            elif msg_type == "image":
                user_msg = msg.get("image", {}).get("caption", "El cliente envió una imagen.")

            # 🎤 Audio (transcripción con Whisper)
            elif msg_type == "audio":
                media_id = msg.get("audio", {}).get("id")
                try:
                    user_msg = transcribe_audio(media_id, WHATSAPP_TOKEN, OPENAI_API_KEY)
                except Exception as e:
                    logging.error(f"Error transcribiendo audio: {e}", exc_info=True)
                    user_msg = "[Audio recibido, pero no se pudo transcribir]"

            # 🔘 Mensajes interactivos (botones, listas)
            elif msg_type == "interactive":
                interactive = msg.get("interactive", {})
                user_msg = (
                    interactive.get("button_reply", {}).get("title")
                    or interactive.get("list_reply", {}).get("title")
                    or "[Interacción recibida]"
                )

            else:
                user_msg = f"[Mensaje tipo {msg_type} no soportado]"

            if not user_msg:
                logging.warning(f"⚠️ Mensaje vacío o no soportado: {msg}")
                return None, None, None, None

            # 💬 Log limpio y claro
            logging.info(f"💬 WhatsApp → {user_id}: {user_msg}")

            return user_id, msg_id, msg_type, user_msg

        except Exception as e:
            logging.error(f"⚠️ ERROR extrayendo datos del mensaje: {e}", exc_info=True)
            return None, None, None, None
