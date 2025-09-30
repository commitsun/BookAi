import os
import asyncio
import random
from datetime import datetime
from dotenv import load_dotenv
from core.graph import app
from core.db import save_message

# Configuración inicial
load_dotenv()
LOGS_DIR = "logs"
os.makedirs(LOGS_DIR, exist_ok=True)

def generate_conversation_id() -> str:
    """Genera un ID ficticio con prefijo 34 y 9 dígitos aleatorios."""
    return "34" + "".join(str(random.randint(0, 9)) for _ in range(9))

async def chat():
    print("💬 Chat HotelAI (escribe 'salir' para terminar)\n")

    conversation_id = generate_conversation_id()
    log_file = os.path.join(LOGS_DIR, f"{conversation_id}.txt")

    print(f"📞 Conversación iniciada con ID: {conversation_id}")
    print(f"📂 Guardando en: {log_file}\n")

    state = {
        "messages": [],
        "route": None,
        "rationale": None,
        "language": None,
        "summary": None,
    }

    with open(log_file, "w", encoding="utf-8", errors="replace") as f:
        f.write(f"Conversación iniciada: {datetime.now()}\n")
        f.write(f"ID: {conversation_id}\n\n")

    while True:
        try:
            user_msg = input("Tú: ")
        except EOFError:
            break

        if user_msg.lower() in ["salir", "exit", "quit"]:
            print("👋 ¡Hasta pronto!")
            break

        # Guardar mensaje del usuario
        state["messages"].append({"role": "user", "content": user_msg})
        save_message(conversation_id, "user", user_msg)

        with open(log_file, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"USER: {user_msg}\n")

        try:
            # Procesar con el grafo
            state = await app.ainvoke(state)
            response = state["messages"][-1]["content"]
            print(f"🤖 {response}\n")

            save_message(conversation_id, "assistant", response)
            with open(log_file, "a", encoding="utf-8", errors="replace") as f:
                f.write(f"ASSISTANT: {response}\n")
        except Exception as e:
            print(f"⚠️ Error: {e}\n")
            with open(log_file, "a", encoding="utf-8", errors="replace") as f:
                f.write(f"ERROR: {e}\n")

if __name__ == "__main__":
    asyncio.run(chat())
