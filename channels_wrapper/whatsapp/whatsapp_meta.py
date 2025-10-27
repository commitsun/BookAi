import asyncio
import logging
import requests
from collections import defaultdict
from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse
from typing import Dict, List
from core.config import Settings as C
from channels_wrapper.base_channel import BaseChannel
from channels_wrapper.utils.media_utils import transcribe_audio
from channels_wrapper.utils.text_utils import send_fragmented_async
from core.escalation_manager import mark_pending

log = logging.getLogger("whatsapp")

BUFFER_WAIT_SECONDS = 8
FRAGMENT_THRESHOLD = 300


class WhatsAppChannel(BaseChannel):
    """Canal WhatsApp (Meta Graph API) con buffer, timers y cancelación."""
    def __init__(self, openai_api_key: str = None):
        super().__init__(openai_api_key=openai_api_key or C.OPENAI_API_KEY)
        self._buffers: Dict[str, List[str]] = defaultdict(list)
        self._timer_tasks: Dict[str, asyncio.Task] = {}
        self._processing_tasks: Dict[str, asyncio.Task] = {}
        self._locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # -----------------------------------------------------------
    # Webhooks
    # -----------------------------------------------------------
    def register_routes(self, app):
        @app.get("/webhook")
        @app.get("/webhook/whatsapp")
        async def verify_webhook(request: Request):
            params = request.query_params
            if (
                params.get("hub.mode") == "subscribe"
                and params.get("hub.verify_token") == C.WHATSAPP_VERIFY_TOKEN
            ):
                log.info("✅ Webhook WhatsApp verificado.")
                return PlainTextResponse(params.get("hub.challenge"), status_code=200)
            return PlainTextResponse("Error de verificación", status_code=403)

        @app.post("/webhook")
        @app.post("/webhook/whatsapp")
        async def whatsapp_webhook(request: Request):
            try:
                data = await request.json()
                asyncio.create_task(self._process_in_background(data))
                return JSONResponse({"status": "ok"})
            except Exception as e:
                log.error(f"❌ Error procesando webhook: {e}", exc_info=True)
                return JSONResponse({"status": "error", "detail": str(e)})

    # -----------------------------------------------------------
    # Buffer y timers
    # -----------------------------------------------------------
    async def _process_in_background(self, data: dict):
        try:
            user_id, msg_id, msg_type, user_message = self.extract_message_data(data)
            if not user_id or not msg_id or not user_message:
                return
            if msg_id in self.processed_ids:
                return
            self.processed_ids.add(msg_id)
            cid = str(user_id).replace("+", "").strip()
            async with self._locks[cid]:
                proc_task = self._processing_tasks.get(cid)
                if proc_task and not proc_task.done():
                    proc_task.cancel()
                self._buffers[cid].append(user_message)
                self._append(cid, "user", user_message)
                await self._restart_timer_locked(cid)
        except Exception as e:
            log.error(f"💥 Error en background: {e}", exc_info=True)

    async def _restart_timer_locked(self, cid: str):
        prev = self._timer_tasks.get(cid)
        if prev and not prev.done():
            prev.cancel()
        self._timer_tasks[cid] = asyncio.create_task(self._timer_then_process(cid))

    async def _timer_then_process(self, cid: str):
        try:
            await asyncio.sleep(BUFFER_WAIT_SECONDS)
            async with self._locks[cid]:
                messages = self._buffers.get(cid, [])
                if not messages:
                    return
                self._buffers[cid] = []
            user_block = self._format_buffer(messages)
            task = asyncio.create_task(self._process_block(cid, user_block))
            self._processing_tasks[cid] = task
            await task
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.error(f"Error en timer/process: {e}", exc_info=True)

    # -----------------------------------------------------------
    # Procesamiento principal
    # -----------------------------------------------------------
    async def _process_block(self, cid: str, user_block: str):
        try:
            if not self.agent:
                log.error("❌ No hay agente asignado.")
                return

            self._append(cid, "user", user_block)
            response = await self.agent.process_message(user_block, cid)

            # 🟡 Si el agente no devuelve nada
            if not response or not response.strip():
                log.warning(f"⚠️ Respuesta vacía para {cid}")
                return

            # 🟢 Enviar SIEMPRE la respuesta al cliente
            await send_fragmented_async(self.send_message, cid, response)
            self._append(cid, "assistant", response)
            log.info(f"📩 Respuesta enviada a {cid}: {response[:100]}")

            # 🔹 Escalación opcional (no bloquea el envío)
            if any(p in response.lower() for p in ["encargado", "consultarlo", "permíteme contactar", "no dispongo"]):
                asyncio.create_task(mark_pending(cid, user_block))

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(f"Error procesando bloque: {e}", exc_info=True)

    # -----------------------------------------------------------
    # Utilidades
    # -----------------------------------------------------------
    def _format_buffer(self, parts: List[str]) -> str:
        cleaned = []
        for p in parts:
            if not p:
                continue
            s = " ".join(p.strip().split())
            if not s:
                continue
            if s[-1] not in ".?!":
                s += "."
            cleaned.append(s)
        return " ".join(cleaned)

    # -----------------------------------------------------------
    # Envío a WhatsApp
    # -----------------------------------------------------------
    def send_message(self, user_id: str, text: str):
        url = f"https://graph.facebook.com/v19.0/{C.WHATSAPP_PHONE_ID}/messages"
        headers = {
            "Authorization": f"Bearer {C.WHATSAPP_TOKEN}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": user_id,
            "type": "text",
            "text": {"body": text},
        }
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=10)
            if r.status_code != 200:
                log.error(f"⚠️ Error WhatsApp ({r.status_code}): {r.text}")
            else:
                log.info(f"🚀 WhatsApp → {user_id}: {text[:80]}... ({r.status_code})")
        except Exception as e:
            log.error(f"⚠️ Error enviando mensaje WhatsApp: {e}", exc_info=True)

    # -----------------------------------------------------------
    # Parser de payload
    # -----------------------------------------------------------
    def extract_message_data(self, payload: dict):
        try:
            entries = payload.get("entry", [])
            if not entries:
                return None, None, None, None
            changes = entries[0].get("changes", [])
            if not changes:
                return None, None, None, None
            value = changes[0].get("value", {})
            messages = value.get("messages", [])
            if not messages:
                return None, None, None, None
            msg = messages[0]
            msg_type = msg.get("type")
            user_id = msg.get("from")
            msg_id = msg.get("id")
            if msg_type == "text":
                user_msg = msg.get("text", {}).get("body", "").strip()
            elif msg_type == "interactive":
                inter = msg.get("interactive", {})
                if "button_reply" in inter:
                    user_msg = inter["button_reply"].get("title", "")
                elif "list_reply" in inter:
                    user_msg = inter["list_reply"].get("title", "")
                else:
                    user_msg = "[Respuesta interactiva]"
            elif msg_type == "audio":
                media_id = msg.get("audio", {}).get("id")
                user_msg = transcribe_audio(media_id, C.WHATSAPP_TOKEN, C.OPENAI_API_KEY)
            elif msg_type == "image":
                user_msg = msg.get("image", {}).get("caption", "Imagen recibida.")
            else:
                user_msg = f"[Tipo de mensaje no soportado: {msg_type}]"
            return user_id, msg_id, msg_type, user_msg
        except Exception as e:
            log.error(f"Error extrayendo datos: {e}", exc_info=True)
            return None, None, None, None
