from abc import ABC, abstractmethod
from openai import OpenAI
from core.graph import app as bot_app
from channels_wrapper.utils.text_utils import fragment_text_intelligently, sleep_typing


class BaseChannel(ABC):
    """
    Clase base para todos los canales (WhatsApp, Telegram, etc.).
    Define la lógica común de conversación, envío de mensajes, y fragmentación.
    """

    def __init__(self, openai_api_key: str):
        self.client = OpenAI(api_key=openai_api_key)
        self.conversations = {}
        self.processed_ids = set()

    # Métodos que las subclases deben implementar
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

    # ------------------------------------------------------------------
    # 🧠 Procesamiento general de mensajes
    # ------------------------------------------------------------------
    async def process_message_async(self, payload: dict):
        """
        Lógica genérica de procesamiento:
        - Deduplica mensajes
        - Llama al bot (core.graph)
        - Envía respuesta fragmentada
        """
        user_id, msg_id, msg_type, user_msg = self.extract_message_data(payload)
        if not user_id or not msg_id:
            return

        if msg_id in self.processed_ids:
            print(f"🔁 Mensaje duplicado ignorado: {msg_id}")
            return
        self.processed_ids.add(msg_id)

        # Inicializa conversación si no existe
        if user_id not in self.conversations:
            self.conversations[user_id] = [
                {"role": "system", "content": (
                    "Eres un asistente virtual de un hotel. "
                    "Responde de forma clara, breve y educada sobre reservas, precios, mascotas, ubicación y servicios."
                )}
            ]

        # Añadir mensaje del usuario
        self.conversations[user_id].append({"role": "user", "content": user_msg})

        # Ejecutar el grafo principal (tu bot)
        state = {"messages": self.conversations[user_id]}
        state = await bot_app.ainvoke(state)
        reply = state["messages"][-1]["content"]

        # Añadir la respuesta del asistente
        self.conversations[user_id].append({"role": "assistant", "content": reply})

        # Fragmentar respuesta y enviar simulando escritura
        fragments = fragment_text_intelligently(reply)
        for frag in fragments:
            sleep_typing(frag)
            self.send_message(user_id, frag)
