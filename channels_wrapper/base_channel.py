# channels_wrapper/base_channel.py
from abc import ABC, abstractmethod
import asyncio
import logging
from openai import OpenAI

from core.main_agent import HotelAIHybrid
from core.message_buffer import MessageBufferManager
from channels_wrapper.utils.text_utils import fragment_text_intelligently


def _simulate_typing_delay(text: str) -> float:
    """
    Devuelve un tiempo de espera 'humano' (asíncrono) según la longitud.
    Evitamos time.sleep para que las tareas sean cancelables.
    """
    base = 1.0
    factor = min(len(text) / 100.0, 3.0)
    return base + factor


class BaseChannel(ABC):
    """
    Clase base para todos los canales (WhatsApp, Telegram, etc.).
    Ahora incluye:
      - Buffer por conversación con timeout de 10s
      - Interrupción/cancelación si entra un mensaje nuevo
      - Envío asíncrono con 'typing delay' cancelable
    """

    def __init__(self, openai_api_key: str):
        self.client = OpenAI(api_key=openai_api_key)
        self.conversations = {}
        self.processed_ids = set()

        # 🤖 Agente (fallback si no lo inyectan desde main.py)
        try:
            self.agent = HotelAIHybrid()
            logging.info("🤖 BaseChannel inicializado con agente HotelAIHybrid interno.")
        except Exception as e:
            logging.warning(f"⚠️ No se pudo inicializar HotelAIHybrid automáticamente: {e}")
            self.agent = None

        # 🧵 Buffer por conversación (10s por defecto)
        self.buffer = MessageBufferManager(idle_seconds=10.0)

    # ============================================================
    # Métodos abstractos (implementados en subclases específicas)
    # ============================================================
    @abstractmethod
    def send_message(self, user_id: str, text: str):
        """Envía un mensaje al usuario final."""
        raise NotImplementedError

    @abstractmethod
    def extract_message_data(self, payload: dict):
        """Extrae información clave del mensaje recibido."""
        raise NotImplementedError

    @abstractmethod
    def register_routes(self, app):
        """Registra las rutas FastAPI específicas del canal."""
        raise NotImplementedError

    # ============================================================
    # 🧠 Procesamiento general con Buffer + Timer + Interrupción
    # ============================================================
    async def process_message_async(self, payload: dict):
        """
        Llega un evento del canal:
          - Deduplica
          - Extrae user_id y texto
          - Encola en el buffer (10s). Cada nuevo mensaje reinicia la cuenta.
          - Al expirar, se procesa TODO el bloque junto.
          - Si entra un mensaje mientras procesa: se cancela y se empieza de nuevo.
        """
        user_id, msg_id, msg_type, user_msg = self.extract_message_data(payload)
        if not user_id or not msg_id:
            logging.debug("📦 Webhook ignorado: evento sin mensaje válido (status update o vacío).")
            return

        # Evitar duplicados de plataforma
        if msg_id in self.processed_ids:
            logging.debug(f"🔁 Mensaje duplicado ignorado: {msg_id}")
            return
        self.processed_ids.add(msg_id)

        if not user_msg:
            return

        conversation_id = str(user_id).replace("+", "").strip()
        logging.info(f"📩 [buffer] {conversation_id}: '{user_msg}'")

        # Encola mensaje y maneja timer/cancelaciones
        await self.buffer.add_message(
            conversation_id=conversation_id,
            text=user_msg,
            process_callback=self._process_batch,  # se llama tras 10s sin nuevos mensajes
        )

    # ------------------------------------------------------------
    # 🔧 Callback: procesa el lote combinado de mensajes
    # ------------------------------------------------------------
    async def _process_batch(self, conversation_id: str, combined_text: str, version: int):
        """
        Se ejecuta cuando expira el temporizador de 10s y no hubo nuevos mensajes.
        Si entra un mensaje nuevo durante este procesamiento, la tarea será cancelada.
        """
        if not self.agent:
            logging.error("❌ No hay agente asignado. No se puede procesar el mensaje.")
            return

        logging.info(f"🧺 Procesando lote (v{version}) para {conversation_id}: {combined_text!r}")

        try:
            reply = await self.agent.process_message(
                user_message=combined_text,
                conversation_id=conversation_id,
            )

            if not reply or not reply.strip():
                logging.warning(f"⚠️ Respuesta vacía del agente para {conversation_id}.")
                return

            # Enviar con fragmentación + delays asíncronos cancelables
            fragments = fragment_text_intelligently(reply)
            for frag in fragments:
                # Si esta tarea fue cancelada (porque entró un mensaje nuevo), lanzará CancelledError
                await asyncio.sleep(_simulate_typing_delay(frag))
                self.send_message(conversation_id, frag)
                logging.info(f"🚀 Enviado a {conversation_id}: {frag[:60]}...")

        except asyncio.CancelledError:
            # Interrupción porque llegó un nuevo mensaje antes de terminar
            logging.info(f"⏹️ Procesamiento cancelado (v{version}) para {conversation_id}")
            return
        except Exception as e:
            logging.error(f"💥 Error procesando lote para {conversation_id}: {e}", exc_info=True)
