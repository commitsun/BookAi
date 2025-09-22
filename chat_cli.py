import asyncio
import logging
from dotenv import load_dotenv
from core.graph import app   # 👈 importamos desde core/graph

# =========
# Configuración
# =========
load_dotenv()

# Configurar logs de conversación con encoding seguro
logging.basicConfig(
    filename="chat_history.log",
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
    encoding="utf-8",  # 👈 aseguramos UTF-8
)

async def chat():
    print("💬 Chat HotelAI (escribe 'salir' para terminar)\n")

    # Estado inicial con memoria vacía
    state = {
        "messages": [],
        "route": None,
        "rationale": None,
        "language": None,
        "summary": None
    }

    while True:
        try:
            user_msg = input("Tú: ")
        except EOFError:
            break

        if user_msg.lower() in ["salir", "exit", "quit"]:
            print("👋 ¡Hasta pronto!")
            break

        # Guardamos mensaje del usuario en memoria
        state["messages"].append({"role": "user", "content": user_msg})
        logging.info(f"USER: {user_msg}")

        try:
            # Invocamos el grafo manteniendo el estado acumulado
            state = await app.ainvoke(state)

            # Respuesta del asistente (saneada para UTF-8)
            response = state["messages"][-1]["content"]
            safe_response = response.encode("utf-8", errors="replace").decode("utf-8")

            print(f"🤖 {safe_response}\n")
            logging.info(f"ASSISTANT: {safe_response}")

        except Exception as e:
            print(f"⚠️ Error: {e}\n")
            logging.error(f"ERROR: {e}")

if __name__ == "__main__":
    asyncio.run(chat())
