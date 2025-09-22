import asyncio
from orquestacion_multiagente import app
from dotenv import load_dotenv

load_dotenv()

async def chat():
    print("💬 Chat HotelAI (escribe 'salir' para terminar)\n")
    while True:
        user_msg = input("Tú: ")
        if user_msg.lower() in ["salir", "exit", "quit"]:
            break

        try:
            result = await app.ainvoke({
                "messages": [{"role": "user", "content": user_msg}],
                "route": None,
                "rationale": None
            })
            response = result["messages"][-1]["content"]
            print(f"🤖 {response}\n")
        except Exception as e:
            print(f"⚠️ Error: {e}\n")

if __name__ == "__main__":
    asyncio.run(chat())
