from abc import ABC, abstractmethod
from openai import OpenAI
from core.main_agent import HotelAIHybrid
from channels_wrapper.utils.text_utils import fragment_text_intelligently, sleep_typing


class BaseChannel(ABC):
    """
    Clase base para todos los canales (WhatsApp, Telegram, etc.).
    Define la lógica común de conversación, envío de mensajes y fragmentación.
    """

    def __init__(self, openai_api_key: str):
        self.client = OpenAI(api_key=openai_api_key)
        self.conversations = {}
        self.processed_ids = set()

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
    # 🧠 Procesamiento general de mensajes
    # ============================================================
    async def process_message_async(self, payload: dict):
        """
        Lógica genérica de procesamiento de mensajes:
        - Deduplica mensajes
        - Llama al agente principal (HotelAIHybrid)
        - Fragmenta y envía la respuesta al usuario
        """
        # Extraer datos esenciales del mensaje entrante
        user_id, msg_id, msg_type, user_msg = self.extract_message_data(payload)
        if not user_id or not msg_id:
            print("⚠️ Mensaje inválido o incompleto, se ignora.")
            return

        # Evitar procesar mensajes duplicados
        if msg_id in self.processed_ids:
            print(f"🔁 Mensaje duplicado ignorado: {msg_id}")
            return
        self.processed_ids.add(msg_id)

        # Limpiar número del usuario (conversation_id)
        conversation_id = str(user_id).replace("+", "").strip()

        # Crear historial local si no existe
        if conversation_id not in self.conversations:
            self.conversations[conversation_id] = [
                {
                    "role": "system",
                    "content": (
                        "Eres un asistente virtual de un hotel. "
                        "Responde de forma clara, amable y profesional sobre reservas, precios, "
                        "mascotas, servicios y ubicación del hotel."
                    ),
                }
            ]

        # Añadir mensaje del usuario al historial
        self.conversations[conversation_id].append({"role": "user", "content": user_msg})
        print(f"📩 Mensaje recibido de {conversation_id}: {user_msg}")

        # --------------------------------------------------------
        # 🤖 Ejecutar el agente híbrido principal
        # --------------------------------------------------------
        agent = HotelAIHybrid()
        reply = await agent.process_message(
            user_message=user_msg,
            conversation_id=conversation_id,  # ✅ se pasa el número del usuario
        )

        # Añadir respuesta del asistente al historial
        self.conversations[conversation_id].append({"role": "assistant", "content": reply})

        # --------------------------------------------------------
        # ✉️ Enviar respuesta fragmentada (simulando escritura)
        # --------------------------------------------------------
        fragments = fragment_text_intelligently(reply)
        for frag in fragments:
            sleep_typing(frag)
            self.send_message(conversation_id, frag)
            print(f"🚀 Enviado a {conversation_id}: {frag[:60]}...")
