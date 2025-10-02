from core.graph import app

def run_fake_whatsapp():
    print("📱 Simulador WhatsApp (Meta API fake) — escribe 'salir' para terminar\n")
    state = {"messages": []}

    while True:
        user_msg = input("👤 Usuario (WhatsApp): ")
        if user_msg.lower() in ["salir", "exit", "quit"]:
            break

        state["messages"].append({"role": "user", "content": user_msg})
        state = app.invoke(state)
        reply = state["messages"][-1]["content"]
        print(f"🤖 Bot (WhatsApp): {reply}\n")
