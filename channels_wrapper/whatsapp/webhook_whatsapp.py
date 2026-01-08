"""Handlers del webhook de WhatsApp (Meta)."""

from __future__ import annotations

import logging
import os

import requests
from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse

from channels_wrapper.utils.text_utils import send_fragmented_async
from core.pipeline import process_user_message

log = logging.getLogger("WhatsAppWebhook")


def _mark_as_read(message_id: str):
    """Envía el status 'read' para reflejar doble check azul en el cliente."""
    phone_id = os.getenv("WHATSAPP_PHONE_ID")
    token = os.getenv("WHATSAPP_TOKEN")
    if not (phone_id and token and message_id):
        log.debug("No se pudo marcar como leído: faltan credenciales o message_id")
        return

    url = f"https://graph.facebook.com/v19.0/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        if resp.status_code >= 400:
            log.warning(
                "⚠️ No se pudo marcar como leído (%s): %s",
                resp.status_code,
                resp.text,
            )
        else:
            log.info("✅ Read receipt enviado (%s)", message_id)
    except Exception as exc:
        log.debug("No se pudo enviar read receipt: %s", exc)


def register_whatsapp_routes(app, state):
    """Registra los endpoints de webhook de WhatsApp en la app FastAPI."""

    @app.get("/webhook")
    async def verify_webhook(request: Request):
        verify_token = os.getenv("WHATSAPP_VERIFY_TOKEN", "midemo")
        params = request.query_params
        if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == verify_token:
            return PlainTextResponse(params.get("hub.challenge"))
        return JSONResponse({"error": "Invalid verification token"}, status_code=403)

    @app.post("/webhook")
    async def whatsapp_webhook(request: Request):
        """Webhook WhatsApp (Meta) + Buffer inteligente + Transcripción de audio (Whisper)."""
        try:
            data = await request.json()
            value = data.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {})
            msg = value.get("messages", [{}])[0]
            contacts = value.get("contacts", [])
            profile = contacts[0].get("profile", {}) if contacts else {}
            client_name = profile.get("name")
            sender = msg.get("from")
            msg_type = msg.get("type")
            msg_id = msg.get("id")

            text = ""

            if msg_id:
                _mark_as_read(msg_id)  # Marca leído para reflejar el doble check azul
                if msg_id in state.processed_whatsapp_ids:
                    log.info("↩️ WhatsApp duplicado ignorado (msg_id=%s)", msg_id)
                    return JSONResponse({"status": "duplicate"})
                if len(state.processed_whatsapp_queue) >= state.processed_whatsapp_queue.maxlen:
                    old = state.processed_whatsapp_queue.popleft()
                    state.processed_whatsapp_ids.discard(old)
                state.processed_whatsapp_queue.append(msg_id)
                state.processed_whatsapp_ids.add(msg_id)

            if msg_type == "text":
                text = msg.get("text", {}).get("body", "")
            elif msg_type == "audio":
                from channels_wrapper.utils.media_utils import transcribe_audio

                media_id = msg.get("audio", {}).get("id")
                if media_id:
                    log.info("🎧 Audio recibido (media_id=%s), iniciando transcripción...", media_id)
                    whatsapp_token = os.getenv("WHATSAPP_TOKEN", "")
                    openai_key = os.getenv("OPENAI_API_KEY", "")
                    text = transcribe_audio(media_id, whatsapp_token, openai_key)
                    log.info("📝 Transcripción completada: %s", text)

            if not sender or not text:
                return JSONResponse({"status": "ignored"})

            log.info("💬 WhatsApp %s: %s", sender, text)
            if client_name:
                state.memory_manager.set_flag(sender, "client_name", client_name)

            async def _process_buffered(cid: str, combined_text: str, version: int):
                log.info(
                    "🧠 Procesando lote buffered v%s → %s\n🧩 Mensajes combinados:\n%s",
                    version,
                    cid,
                    combined_text,
                )
                resp = await process_user_message(combined_text, cid, state=state, channel="whatsapp")

                if not resp:
                    log.info("🔇 Escalación silenciosa %s", cid)
                    return

                async def send_to_channel(uid: str, txt: str):
                    await state.channel_manager.send_message(uid, txt, channel="whatsapp")

                await send_fragmented_async(send_to_channel, cid, resp)

            await state.buffer_manager.add_message(sender, text, _process_buffered)

            return JSONResponse({"status": "queued"})

        except Exception as exc:
            log.error("❌ Error en webhook WhatsApp: %s", exc, exc_info=True)
            return JSONResponse({"status": "error"}, status_code=500)
