import re
import time
import random
import asyncio
from openai import OpenAI
from core.graph import app as bot_app


class BaseChannel:
    """Clase base para todos los canales."""

    def __init__(self, openai_api_key: str):
        self.client = OpenAI(api_key=openai_api_key)
        self.conversations = {}
        self.processed_ids = set()

    # ------------------------------------------------------------------
    # --- Métodos abstractos ---
    # ------------------------------------------------------------------
    def send_message(self, user_id: str, text: str):
        raise NotImplementedError

    def extract_message_data(self, payload: dict):
        """Extrae user_id, msg_id, msg_type y texto del payload específico del canal."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # --- Lógica común ---
    # ------------------------------------------------------------------
    def fragment_text_intelligently(self, text: str) -> list[str]:
        text = re.sub(r'\n{2,}', '\n', text.strip())
        raw_parts = re.split(r'(?:(?<=\n)\d+\.|\n-|\n•|\n(?=[A-Z]))', text)
        fragments, buffer = [], ""

        for part in raw_parts:
            p = part.strip()
            if not p:
                continue
            if re.match(r'^(\d+\.|-|•)\s', p):
                if buffer:
                    fragments.append(buffer.strip())
                    buffer = ""
                fragments.append(p)
                continue
            if len(p) > 500:
                subparts = re.split(r'(?<=[.!?])\s+', p)
                temp_chunk = ""
                for s in subparts:
                    if len(temp_chunk) + len(s) < 300:
                        temp_chunk += (" " if temp_chunk else "") + s
                    else:
                        fragments.append(temp_chunk.strip())
                        temp_chunk = s
                if temp_chunk:
                    fragments.append(temp_chunk.strip())
            else:
                if len(buffer) + len(p) < 400:
                    buffer += ("\n\n" if buffer else "") + p
                else:
                    fragments.append(buffer.strip())
                    buffer = p

        if buffer:
            fragments.append(buffer.strip())

        if len(fragments) > 4:
            merged, temp = [], ""
            for f in fragments:
                if len(temp) + len(f) < 500:
                    temp += ("\n\n" if temp else "") + f
                else:
                    merged.append(temp)
                    temp = f
            if temp:
                merged.append(temp)
            fragments = merged[:4]

        return fragments

    async def process_message_async(self, payload: dict):
        """Procesa un mensaje recibido desde el canal."""
        try:
            user_id, msg_id, msg_type, user_msg = self.extract_message_data(payload)
            if not user_id or not msg_id:
                print("⚠️ Payload inválido, sin user_id o msg_id")
                return

            if msg_id in self.processed_ids:
                print(f"🔁 Mensaje duplicado ignorado: {msg_id}")
                return

            self.processed_ids.add(msg_id)
            if len(self.processed_ids) > 5000:
                self.processed_ids = set(list(self.processed_ids)[-2000:])

            if user_id not in self.conversations:
                self.conversations[user_id] = [
                    {
                        "role": "system",
                        "content": (
                            "Eres un asistente virtual de un hotel. "
                            "Responde de forma clara, breve y educada a las preguntas del cliente "
                            "sobre disponibilidad, precios, mascotas, ubicación, reservas y servicios."
                        )
                    }
                ]

            self.conversations[user_id].append({"role": "user", "content": user_msg})

            state = {"messages": self.conversations[user_id]}
            state = await bot_app.ainvoke(state)
            reply = state["messages"][-1]["content"]

            self.conversations[user_id].append({"role": "assistant", "content": reply})
            self.send_message(user_id, reply)

        except Exception as e:
            print("⚠️ Error procesando mensaje:", e)
