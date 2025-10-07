from abc import ABC, abstractmethod
from openai import OpenAI
from core.graph import app as bot_app

class BaseChannel(ABC):
    """Define la interfaz y la lógica común a todos los canales."""

    def __init__(self, openai_api_key: str):
        self.client = OpenAI(api_key=openai_api_key)
        self.conversations = {}
        self.processed_ids = set()

    @abstractmethod
    def send_message(self, user_id: str, text: str): ...
    @abstractmethod
    def extract_message_data(self, payload: dict): ...
    @abstractmethod
    def register_routes(self, app): ...

    async def process_message_async(self, payload: dict):
        user_id, msg_id, msg_type, user_msg = self.extract_message_data(payload)
        if not user_id or not msg_id:
            return

        if msg_id in self.processed_ids:
            print(f"🔁 Mensaje duplicado ignorado: {msg_id}")
            return
        self.processed_ids.add(msg_id)

        if user_id not in self.conversations:
            self.conversations[user_id] = [
                {"role": "system", "content": (
                    "Eres un asistente virtual de un hotel. "
                    "Responde de forma clara, breve y educada."
                )}
            ]

        self.conversations[user_id].append({"role": "user", "content": user_msg})
        state = {"messages": self.conversations[user_id]}
        state = await bot_app.ainvoke(state)
        reply = state["messages"][-1]["content"]
        self.conversations[user_id].append({"role": "assistant", "content": reply})

        self.send_message(user_id, reply)
