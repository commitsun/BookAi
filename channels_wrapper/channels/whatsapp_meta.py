import os
import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from core.graph import app as bot_app

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "midemo")

fastapi_app = FastAPI()

# 🔹 Verificación inicial con Meta
@fastapi_app.get("/webhook")
async def verify_webhook(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return PlainTextResponse(challenge, status_code=200)
    return PlainTextResponse("Error de verificación", status_code=403)

# 🔹 Recepción de mensajes
@fastapi_app.post("/webhook")
async def whatsapp_webhook(request: Request):
    data = await request.json()

    try:
        entry = data["entry"][0]["changes"][0]["value"]

        if "messages" in entry:
            user_msg = entry["messages"][0]["text"]["body"]
            user_id = entry["messages"][0]["from"]

            # Procesar mensaje con el bot
            state = {"messages": [{"role": "user", "content": user_msg}]}
            state = await bot_app.ainvoke(state)   # 👈 async

            reply = state["messages"][-1]["content"]

            # Enviar respuesta a WhatsApp
            send_message(user_id, reply)

    except Exception as e:
        print("⚠️ Error en webhook:", e)

    return JSONResponse({"status": "ok"})

def send_message(to: str, text: str):
    url = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    r = requests.post(url, headers=headers, json=payload)
    print("Respuesta de Meta:", r.status_code, r.text)
