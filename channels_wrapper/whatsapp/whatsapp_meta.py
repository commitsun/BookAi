import os
import json
import asyncio
import logging
import requests
from collections import defaultdict
from typing import Dict, List

from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse

from channels_wrapper.base_channel import BaseChannel
from channels_wrapper.utils.media_utils import transcribe_audio
from channels_wrapper.utils.text_utils import fragment_text_intelligently, sleep_typing
from core.escalation_manager import mark_pending

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

BUFFER_WAIT_SECONDS = 8   # ⏳ Ventana de escritura del cliente antes de procesar
FRAGMENT_THRESHOLD = 300  # ✂️ Fragmentar respuestas largas


class WhatsAppChannel(BaseChannel):
    """Canal WhatsApp (Meta Graph API) — manejo limpio de mensajes con buffer + timer + cancelación."""
    def __init__(self, openai_api_key: str = None):
        super().__init__(openai_api_key=openai_api_key or OPENAI_API_KEY)

        # 🧃 Buffers de entrada por usuario
        self._buffers: Dict[str, List[str]] = defaultdict(list)
        # ⏱️ Timers por usuario (para esperar a que el cliente termine de escribir)
        self._timer_tasks: Dict[str, asyncio.Task] = {}
        # ⚙️ Tareas de procesamiento activas por usuario (para poder cancelarlas)
        self._processing_tasks: Dict[str, asyncio.Task] = {}
        # 🔒 Locks por usuario para evitar condiciones de carrera
        self._locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # =====================================================
    # Registro del webhook
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
                # Procesar en segundo plano para responder rápido a Meta
                asyncio.create_task(self._process_in_background(data))
                return JSONResponse({"status": "ok"})
            except Exception as e:
                logging.error(f"❌ Error procesando webhook: {e}", exc_info=True)
                return JSONResponse({"status": "error", "detail": str(e)})

    # =====================================================
    # 🧠 Lógica: buffer + timer + cancelación
    # =====================================================
    async def _process_in_background(self, data: dict):
        try:
            user_id, msg_id, msg_type, user_message = self.extract_message_data(data)
            if not user_id or not msg_id:
                logging.debug("📦 Webhook ignorado: evento sin mensaje válido (status update o vacío).")
                return

            # Deduplicación
            if msg_id in self.processed_ids:
                logging.debug(f"🔁 Mensaje duplicado ignorado: {msg_id}")
                return
            self.processed_ids.add(msg_id)

            # Mensajes vacíos o no soportados
            if not user_message:
                logging.warning("⚠️ Mensaje vacío o inválido.")
                return

            conversation_id = str(user_id).replace("+", "").strip()

            async with self._locks[conversation_id]:
                # 🛑 Si hay una respuesta en curso, la cancelamos porque el cliente ha enviado algo nuevo
                proc_task = self._processing_tasks.get(conversation_id)
                if proc_task and not proc_task.done():
                    logging.info(f"🛑 Cancelando procesamiento en curso para {conversation_id} por nuevo mensaje.")
                    proc_task.cancel()

                # 🧃 Acumular en el buffer
                self._buffers[conversation_id].append(user_message)
                self._append_to_conversation(conversation_id, "user", user_message)
                logging.info(f"📩 Buffer[{conversation_id}] ← {user_message!r} (len={len(self._buffers[conversation_id])})")

                # ⏱️ Reiniciar / arrancar temporizador
                await self._restart_timer_locked(conversation_id)

        except Exception as e:
            logging.error(f"💥 Error en background WhatsApp: {e}", exc_info=True)

    async def _restart_timer_locked(self, conversation_id: str):
        """Reinicia el temporizador para este usuario; al expirar, procesa el buffer."""
        # Cancelar timer anterior si existe
        prev_timer = self._timer_tasks.get(conversation_id)
        if prev_timer and not prev_timer.done():
            prev_timer.cancel()

        # Lanzar nuevo timer
        self._timer_tasks[conversation_id] = asyncio.create_task(
            self._timer_then_process(conversation_id)
        )
        logging.debug(f"⏳ Timer reiniciado para {conversation_id} ({BUFFER_WAIT_SECONDS}s).")

    async def _timer_then_process(self, conversation_id: str):
        """Espera la ventana y luego procesa el bloque acumulado."""
        try:
            await asyncio.sleep(BUFFER_WAIT_SECONDS)

            # Recoger y vaciar el buffer de forma atómica
            async with self._locks[conversation_id]:
                messages = self._buffers.get(conversation_id, [])
                if not messages:
                    return
                # Limpiamos buffer para no reusar si se cancela luego
                self._buffers[conversation_id] = []

            # Formatear bloque coherente (una sola cadena)
            user_block = self._format_buffer_for_agent(messages)
            logging.info(f"🧾 Bloque a procesar [{conversation_id}]: {user_block!r}")

            # Procesar en tarea cancelable
            task = asyncio.create_task(self._process_block(conversation_id, user_block))
            self._processing_tasks[conversation_id] = task
            await task

        except asyncio.CancelledError:
            logging.debug(f"⏹️ Timer cancelado para {conversation_id}.")
            return
        except Exception as e:
            logging.error(f"⚠️ Error en timer/process para {conversation_id}: {e}", exc_info=True)

    async def _process_block(self, conversation_id: str, user_block: str):
        """Llama al agente con el bloque unido y envía la respuesta (con posible fragmentación), salvo cancelación."""
        from main import hybrid_agent  # Usar el agente global inyectado en main.py

        try:
            if not hybrid_agent:
                logging.error("❌ No hay hybrid_agent disponible.")
                return

            # Guardar en historial ligero y procesar
            self._append_to_conversation(conversation_id, "user", user_block)

            response = await hybrid_agent.process_message(user_block, conversation_id)

            if not response or not response.strip():
                logging.warning(f"⚠️ El agente devolvió respuesta vacía para {conversation_id}.")
                return

            # Escalación automática (fallback a encargado)
            if any(p in response.lower() for p in [
                "contactar con el encargado",
                "consultarlo con el encargado",
                "voy a consultarlo con el encargado",
                "un momento por favor",
                "permíteme contactar",
                "he contactado con el encargado",
                "no dispongo",  
                "error",
            ]):
                await mark_pending(conversation_id, user_block)
                logging.info(f"🕓 Escalando conversación con {conversation_id}")
                return


            # ✂️ Enviar con fragmentación solo si es largo
            if len(response) >= FRAGMENT_THRESHOLD:
                fragments = fragment_text_intelligently(response)
                for frag in fragments:
                    sleep_typing(frag)
                    self.send_message(conversation_id, frag)
                    logging.info(f"🚀 Enviado (fragmento) a {conversation_id}: {frag[:80]}...")
            else:
                self.send_message(conversation_id, response)

            self._append_to_conversation(conversation_id, "assistant", response)

        except asyncio.CancelledError:
            logging.info(f"🛑 Procesamiento cancelado para {conversation_id} (nuevo mensaje llegó).")
            # No enviamos nada, simplemente salimos
            raise
        except Exception as e:
            logging.error(f"💥 Error procesando bloque para {conversation_id}: {e}", exc_info=True)

    # =====================================================
    # 🧾 Formateo del buffer antes de enviar al agente
    # =====================================================
    def _format_buffer_for_agent(self, parts: List[str]) -> str:
        """
        Une los trozos en un único bloque fluido.
        Reglas:
        - Mantener orden.
        - Limpiar espacios.
        - Insertar punto final si no hay . ? !
        """
        cleaned: List[str] = []
        for p in parts:
            if not p:
                continue
            s = " ".join(p.strip().split())
            if not s:
                continue
            if s[-1] not in ".?!":
                s = s + "."
            cleaned.append(s)
        return " ".join(cleaned)

    # =====================================================
    # Envío a WhatsApp
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
    # Parser de payload Meta (robusto)
    # =====================================================
    def extract_message_data(self, payload: dict):
        """Extrae user_id, msg_id, tipo y texto del mensaje entrante (robusto)."""
        try:
            entries = payload.get("entry", [])
            if not entries:
                logging.warning("⚠️ Payload sin 'entry'.")
                return None, None, None, None

            changes = entries[0].get("changes", [])
            if not changes:
                logging.warning("⚠️ Payload sin 'changes'.")
                return None, None, None, None

            value = changes[0].get("value", {})
            messages = value.get("messages", [])

            # Si no hay 'messages', puede ser un 'status update' (entregas, lecturas)
            if not messages:
                statuses = value.get("statuses", [])
                if statuses:
                    logging.info("ℹ️ Webhook de estado (no mensaje de usuario).")
                else:
                    logging.warning("⚠️ Webhook sin mensajes ni estados.")
                return None, None, None, None

            msg = messages[0]
            msg_type = msg.get("type")
            user_id = msg.get("from")
            msg_id = msg.get("id")

            # Extraer contenido según tipo
            if msg_type == "text":
                user_msg = msg.get("text", {}).get("body", "").strip()

            elif msg_type == "interactive":
                # Cuando el usuario responde a botones o listas
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
            return user_id, msg_id, msg_type, user_msg or None

        except Exception as e:
            logging.error(f"⚠️ Error extrayendo datos del mensaje: {e}", exc_info=True)
            return None, None, None, None
