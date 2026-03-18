# channels_wrapper/base_channel.py
from abc import ABC, abstractmethod
from typing import Dict, List, Tuple, Optional
import logging
from openai import OpenAI
from core.main_agent import create_main_agent
from core.memory_manager import MemoryManager
from core.config import Settings as C
from channels_wrapper.utils.text_utils import send_fragmented_async

log = logging.getLogger("channel")


# Plantilla base común para todos los canales.
# Se usa en el flujo de capa base de canales y procesamiento común de mensajes como pieza de organización, contrato de datos o punto de extensión.
# Se instancia con configuración, managers, clients o callbacks externos y luego delega el trabajo en sus métodos.
# Los efectos reales ocurren cuando sus métodos se invocan; la definición de clase solo organiza estado y responsabilidades.
class BaseChannel(ABC):
    """Plantilla base común para todos los canales."""

    # Inicializa el estado interno y las dependencias de `BaseChannel`.
    # Se usa dentro de `BaseChannel` en el flujo de capa base de canales y procesamiento común de mensajes.
    # Recibe `openai_api_key` como dependencias o servicios compartidos inyectados desde otras capas.
    # No devuelve valor; deja la instancia preparada con sus dependencias y estado inicial. Puede realizar llamadas externas o a modelos.
    def __init__(self, openai_api_key: Optional[str] = None):
        self.client = OpenAI(api_key=openai_api_key or C.OPENAI_API_KEY)
        self.conversations: Dict[str, List[dict]] = {}
        self.processed_ids: set[str] = set()

        # 🔧 Inicializamos el nuevo MainAgent
        try:
            self.memory_manager = MemoryManager()
            self.agent = create_main_agent(memory_manager=self.memory_manager)
            log.info("🤖 BaseChannel inicializado con MainAgent (refactor).")
        except Exception as e:
            log.warning(f"⚠️ No se pudo inicializar MainAgent: {e}")
            self.agent = None

    # Envía el mensaje.
    # Se usa dentro de `BaseChannel` en el flujo de capa base de canales y procesamiento común de mensajes.
    # Recibe `user_id`, `text` como entradas relevantes junto con el contexto inyectado en la firma.
    # Produce la acción solicitada y prioriza el efecto lateral frente a un retorno complejo. Sin efectos secundarios relevantes.
    @abstractmethod
    def send_message(self, user_id: str, text: str):
        ...

    # Extrae mensaje data.
    # Se usa dentro de `BaseChannel` en el flujo de capa base de canales y procesamiento común de mensajes.
    # Recibe `payload` como entrada principal según la firma.
    # No devuelve un valor relevante; deja preparado el estado o ejecuta la acción necesaria. Sin efectos secundarios relevantes.
    @abstractmethod
    def extract_message_data(
        self, payload: dict
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        ...

    # Registra las rutas de `` sobre la aplicación FastAPI activa.
    # Se usa dentro de `BaseChannel` en el flujo de capa base de canales y procesamiento común de mensajes.
    # Recibe `app` como dependencias o servicios compartidos inyectados desde otras capas.
    # No devuelve un valor de negocio; deja aplicado el cambio de estado o registro correspondiente. Sin efectos secundarios relevantes.
    @abstractmethod
    def register_routes(self, app):
        ...

    # Extrae los datos clave del payload recibido desde Telegram.
    # Se usa dentro de `BaseChannel` en el flujo de capa base de canales y procesamiento común de mensajes.
    # Recibe `payload` como entrada principal según la firma.
    # Devuelve el resultado calculado para que el siguiente paso lo consuma. Sin efectos secundarios relevantes.
    def extract_message_data(self, payload: dict):
        """
        Extrae los datos clave del payload recibido desde Telegram.
        Devuelve: (user_id, message_id, message_type, message_text)
        """
        try:
            message = payload.get("message", {})
            chat = message.get("chat", {})
            user_id = str(chat.get("id", "")) or None
            message_id = str(message.get("message_id", "")) or None
            message_type = "text"
            message_text = (message.get("text") or "").strip() or None

            return user_id, message_id, message_type, message_text
        except Exception as e:
            log.error(f"⚠️ Error extrayendo datos del mensaje de Telegram: {e}", exc_info=True)
            return None, None, None, None

    # Resuelve el proceso.
    # Se usa dentro de `BaseChannel` en el flujo de capa base de canales y procesamiento común de mensajes.
    # Recibe `conversation_id`, `msg_type`, `user_msg` como entradas relevantes junto con el contexto inyectado en la firma.
    # No devuelve un valor relevante; deja preparado el estado o ejecuta la acción necesaria. Sin efectos secundarios relevantes.
    async def pre_process(self, conversation_id: str, msg_type: str, user_msg: str):
        pass

    # Resuelve el proceso.
    # Se usa dentro de `BaseChannel` en el flujo de capa base de canales y procesamiento común de mensajes.
    # Recibe `conversation_id`, `reply` como entradas relevantes junto con el contexto inyectado en la firma.
    # No devuelve un valor relevante; deja preparado el estado o ejecuta la acción necesaria. Sin efectos secundarios relevantes.
    async def post_process(self, conversation_id: str, reply: str):
        pass

    # Procesa mensaje asincronía.
    # Se usa dentro de `BaseChannel` en el flujo de capa base de canales y procesamiento común de mensajes.
    # Recibe `payload` como entrada principal según la firma.
    # Produce la acción solicitada y prioriza el efecto lateral frente a un retorno complejo. Puede enviar mensajes o plantillas, realizar llamadas externas o a modelos.
    async def process_message_async(self, payload: dict):
        user_id, msg_id, msg_type, user_msg = self.extract_message_data(payload)
        if not user_id or not msg_id or not user_msg:
            return

        # Evitar duplicados
        if msg_id in self.processed_ids:
            log.debug(f"Duplicado ignorado: {msg_id}")
            return
        self.processed_ids.add(msg_id)

        cid = str(user_id).replace("+", "").strip()
        self._append(cid, "user", user_msg)
        await self.pre_process(cid, msg_type, user_msg)

        if not self.agent:
            log.error("❌ No hay agente asignado.")
            return

        try:
            # 🧠 Invocamos el nuevo MainAgent refactorizado
            reply = await self.agent.ainvoke(
                user_input=user_msg,
                chat_id=cid,
                hotel_name="Hotel",
                chat_history=self.conversations.get(cid, []),
            )

            if not reply or not reply.strip():
                log.warning(f"⚠️ Respuesta vacía para {cid}")
                return

            self._append(cid, "assistant", reply)
            await send_fragmented_async(self.send_message, cid, reply)
            await self.post_process(cid, reply)

        except Exception as e:
            log.error(f"💥 Error procesando mensaje: {e}", exc_info=True)
            await self.send_message(
                cid,
                "❌ Lo siento, ha ocurrido un problema al procesar tu mensaje. Intenta de nuevo más tarde."
            )

    # Asegura el ensure.
    # Se usa dentro de `BaseChannel` en el flujo de capa base de canales y procesamiento común de mensajes.
    # Recibe `cid` como entrada principal según la firma.
    # No devuelve un valor relevante; deja preparado el estado o ejecuta la acción necesaria. Sin efectos secundarios relevantes.
    def _ensure(self, cid: str):
        if cid not in self.conversations:
            self.conversations[cid] = [
                {
                    "role": "system",
                    "content": (
                        "Eres un asistente virtual de un hotel. "
                        "Responde de forma clara, amable y profesional."
                    ),
                }
            ]

    # Añade el append.
    # Se usa dentro de `BaseChannel` en el flujo de capa base de canales y procesamiento común de mensajes.
    # Recibe `cid`, `role`, `content` como entradas relevantes junto con el contexto inyectado en la firma.
    # No devuelve un valor relevante; deja preparado el estado o ejecuta la acción necesaria. Sin efectos secundarios relevantes.
    def _append(self, cid: str, role: str, content: str):
        self._ensure(cid)
        self.conversations[cid].append({"role": role, "content": content})
