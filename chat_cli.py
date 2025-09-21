import asyncio
from orquestacion_multiagente import app

async def chat():
    print("💬 Chat HotelAI (escribe 'salir' para terminar)\n")
    while True:
        user_msg = input("Tú: ")
        if user_msg.lower() in ["salir", "exit", "quit"]:
            break

        result = app.invoke({"messages": [{"role": "user", "content": user_msg}], "route": None})
        response = result["messages"][-1]["content"]

        print(f"🤖 {response}\n")

if __name__ == "__main__":
    asyncio.run(chat())
