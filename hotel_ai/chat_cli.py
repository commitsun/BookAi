import asyncio

async def chat():
    print("💬 Chat HotelAI (escribe 'salir' para terminar)\n")
    while True:
        user_msg = input("Tú: ")
        if user_msg.lower() in ["salir", "exit", "quit"]:
            break

        # 🔹 Aquí simularíamos orquestación del MainAgent
        if "reserva" in user_msg or "precio" in user_msg:
            response = "MainAgent -> DispoPreciosAgent: 'Habitación estándar disponible por 200€.'"
        elif "mascota" in user_msg or "piscina" in user_msg:
            response = "MainAgent -> InfoAgent: 'No se permiten mascotas en el hotel.'"
        else:
            response = "MainAgent -> InternoAgent: 'He avisado al encargado, esperando respuesta.'"

        print(f"🤖 {response}\n")

if __name__ == "__main__":
    asyncio.run(chat())
