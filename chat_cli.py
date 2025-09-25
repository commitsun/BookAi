import os
import asyncio
import random
from datetime import datetime
from dotenv import load_dotenv
from core.graph import app
from core.db import save_message

# =========
# Configuración
# =========
load_dotenv()

# Crear carpeta logs si no existe
LOGS_DIR = "logs"
os.makedirs(LOGS_DIR, exist_ok=True)

# =========
# Generador de ID de conversación (ejemplo: 34 + 9 dígitos aleatorios)
# =========
def generate_conversation_id():
    prefix = "34"  # prefijo de teléfono ficticio
    random_digits = "".join([str(random.randint(0, 9)) for _ in range(9)])
    return prefix + random_digits

# =========
# Chat principal
# =========
async def chat():
    print("💬 Chat HotelAI (escribe 'salir' para terminar)\n")

    # Generar ID de conversación artificial
    conversation_id = generate_conversation_id()
    log_file = os.path.join(LOGS_DIR, f"{conversation_id}.txt")

    print(f"📞 Conversación iniciada con ID: {conversation_id}")
    print(f"📂 Guardando en: {log_file}\n")

    # Estado inicial
    state = {
        "messages": [],
        "route": None,
        "rationale": None,
        "language": None,
        "summary": None,
    }

    # Abrir archivo log
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

        # Guardar en estado y en Supabase
        state["messages"].append({"role": "user", "content": user_msg})
        save_message(conversation_id, "user", user_msg)

        # Guardar también en archivo log
        with open(log_file, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"USER: {user_msg}\n")

        try:
            # Invocar el grafo
            state = await app.ainvoke(state)

            # Respuesta del asistente
            response = state["messages"][-1]["content"]
            print(f"🤖 {response}\n")

            # Guardar en Supabase
            save_message(conversation_id, "assistant", response)

            # Guardar también en archivo log
            with open(log_file, "a", encoding="utf-8", errors="replace") as f:
                f.write(f"ASSISTANT: {response}\n")

        except Exception as e:
            print(f"⚠️ Error: {e}\n")
            with open(log_file, "a", encoding="utf-8", errors="replace") as f:
                f.write(f"ERROR: {e}\n")

if __name__ == "__main__":
    asyncio.run(chat())
