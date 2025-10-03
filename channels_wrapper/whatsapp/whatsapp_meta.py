import os
import requests
import json
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from core.graph import app as bot_app
import openai

# --- Configuración ---
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "midemo")

openai.api_key = os.getenv("OPENAI_API_KEY")

fastapi_app = FastAPI()

# 🔹 Guardar conversaciones en memoria (diccionario por usuario)
conversations = {}


# --- Verificación inicial con Meta ---
@fastapi_app.get("/webhook")
async def verify_webhook(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return PlainTextResponse(challenge, status_code=200)
    return PlainTextResponse("Error de verificación", status_code=403)


# --- Webhook mensajes entrantes ---
@fastapi_app.post("/webhook")
async def whatsapp_webhook(request: Request):
    data = await request.json()
    print("📩 Payload recibido:\n", json.dumps(data, indent=2, ensure_ascii=False))

    try:
        entry = data["entry"][0]["changes"][0]["value"]

        if "messages" not in entry:
            return JSONResponse({"status": "ok"})

        msg = entry["messages"][0]
        msg_type = msg.get("type")
        user_id = msg["from"]

        # 🔹 Inicializar historial del usuario si no existe
        if user_id not in conversations:
            conversations[user_id] = [
                {
                    "role": "system",
                    "content": (
                        "Eres un asistente virtual de un hotel. "
                        "Responde de forma clara, breve y educada a las preguntas del cliente "
                        "sobre disponibilidad, precios, mascotas, ubicación, reservas y servicios. "
                        "No devuelvas análisis ni explicaciones, solo responde directamente."
                    )
                }
            ]

        # 📝 Extraer texto del mensaje según tipo
        if msg_type == "text":
            user_msg = msg["text"]["body"]

        elif msg_type == "image":
            media_id = msg["image"]["id"]
            caption = msg["image"].get("caption", "")
            file = download_media(media_id, "imagen.jpg")
            user_msg = caption if file else "El cliente envió una imagen (no se pudo descargar)."

        elif msg_type == "audio":
            media_id = msg["audio"]["id"]
            file = download_media(media_id, "nota.ogg")
            user_msg = transcribir_audio(file) if file else "Error al procesar audio."

        else:
            user_msg = f"[Mensaje tipo {msg_type} no soportado]"

        # 🔹 Guardar mensaje del usuario en el historial
        conversations[user_id].append({"role": "user", "content": user_msg})

        # 🚀 Pasar historial completo al bot
        state = {"messages": conversations[user_id]}
        state = await bot_app.ainvoke(state)
        reply = state["messages"][-1]["content"]

        # 🔹 Guardar respuesta en el historial
        conversations[user_id].append({"role": "user", "content": user_msg})
        conversations[user_id].append({"role": "assistant", "content": reply})
        

        # 📤 Enviar respuesta al usuario
        send_message(user_id, reply)

    except Exception as e:
        print("⚠️ Error en webhook:", e)

    return JSONResponse({"status": "ok"})


# --- Funciones auxiliares ---
def download_media(media_id: str, filename: str):
    """Descarga imagen/audio desde WhatsApp Graph API"""
    url = f"https://graph.facebook.com/v19.0/{media_id}"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    r = requests.get(url, headers=headers)

    if r.status_code != 200:
        print(f"❌ Error obteniendo URL de media {media_id}: {r.text}")
        return None

    media_url = r.json().get("url")
    if not media_url:
        print("❌ No se encontró URL en respuesta de Meta.")
        return None

    r = requests.get(media_url, headers=headers)
    if r.status_code == 200:
        with open(filename, "wb") as f:
            f.write(r.content)
        print(f"✅ Archivo guardado en {filename}")
        return filename

    print(f"❌ Error descargando media {media_id}: {r.text}")
    return None


def transcribir_audio(filepath: str) -> str:
    """Transcribe un audio usando Whisper de OpenAI"""
    if not filepath or not os.path.exists(filepath):
        return "[Error: no se pudo descargar el audio]"

    with open(filepath, "rb") as f:
        transcript = openai.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            prompt="Pregunta de un cliente sobre un hotel. Transcribe lo más claro posible."
        )
    texto = transcript.text.strip()
    print(f"📝 Transcripción: {texto}")
    return texto or "[Audio vacío]"


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
    print("📤 Respuesta de Meta:", r.status_code, r.text)
