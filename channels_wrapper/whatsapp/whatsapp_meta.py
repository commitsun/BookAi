import os
import requests
import json
import re
import time
import random
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from core.graph import app as bot_app
from openai import OpenAI

# --- Configuración ---
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "midemo")

# 🔑 Inicializa el cliente de OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

fastapi_app = FastAPI()

# 🔹 Guardar conversaciones en memoria (diccionario por usuario)
conversations = {}

# ------------------------------------------------------------------
# 🧩 FUNCIÓN: Fragmentar texto de forma natural
# ------------------------------------------------------------------
def fragment_text_intelligently(text: str) -> list[str]:
    """
    Divide el texto en fragmentos naturales y legibles:
    conserva listas y formato, evitando mensajes demasiado cortos o largos.
    """
    text = re.sub(r'\n{2,}', '\n', text.strip())
    raw_parts = re.split(r'(?:(?<=\n)\d+\.|\n-|\n•|\n(?=[A-Z]))', text)

    fragments = []
    buffer = ""

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
        merged = []
        temp = ""
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


# ------------------------------------------------------------------
# 🧩 FUNCIÓN: Enviar mensajes simulando escritura humana
# ------------------------------------------------------------------
def send_message(to: str, text: str):
    """Envía un mensaje dividido en fragmentos, simulando escritura humana."""
    url = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }

    fragments = fragment_text_intelligently(text)

    for i, frag in enumerate(fragments):
        typing_payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "typing_on"
        }
        requests.post(url, headers=headers, json=typing_payload)

        delay = random.uniform(1.5, 3.5) if len(frag) < 80 else random.uniform(3.0, 5.0)
        time.sleep(delay)

        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": frag}
        }
        r = requests.post(url, headers=headers, json=payload)
        print(f"📤 Enviado fragmento {i+1}/{len(fragments)} ({r.status_code})")

        stop_typing_payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "typing_off"
        }
        requests.post(url, headers=headers, json=stop_typing_payload)

        time.sleep(random.uniform(0.5, 1.5))


# ------------------------------------------------------------------
# --- Webhook de verificación ---
# ------------------------------------------------------------------
@fastapi_app.get("/webhook")
async def verify_webhook(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return PlainTextResponse(challenge, status_code=200)
    return PlainTextResponse("Error de verificación", status_code=403)


# ------------------------------------------------------------------
# --- Webhook mensajes entrantes ---
# ------------------------------------------------------------------
@fastapi_app.post("/webhook")
async def whatsapp_webhook(request: Request):
    data = await request.json()
    print("📩 Payload recibido:\n", json.dumps(data, indent=2, ensure_ascii=False))

    try:
        entry = data.get("entry", [])[0]["changes"][0]["value"]

        if "messages" not in entry:
            return JSONResponse({"status": "ok"})

        msg = entry["messages"][0]
        msg_type = msg.get("type")
        user_id = msg["from"]
        msg_id = msg.get("id")

        if hasattr(whatsapp_webhook, "_last_msg_id") and whatsapp_webhook._last_msg_id == msg_id:
            print("🔁 Mensaje duplicado ignorado.")
            return JSONResponse({"status": "duplicate_ignored"})
        whatsapp_webhook._last_msg_id = msg_id

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

        conversations[user_id].append({"role": "user", "content": user_msg})

        state = {"messages": conversations[user_id]}
        state = await bot_app.ainvoke(state)
        reply = state["messages"][-1]["content"]

        conversations[user_id].append({"role": "assistant", "content": reply})

        send_message(user_id, reply)

    except Exception as e:
        print("⚠️ Error en webhook:", e)

    return JSONResponse({"status": "ok"})


# ------------------------------------------------------------------
# --- Funciones auxiliares ---
# ------------------------------------------------------------------
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
    try:
        if not filepath or not os.path.exists(filepath):
            return "[Error: no se pudo descargar el audio]"

        with open(filepath, "rb") as f:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                prompt="Pregunta de un cliente sobre un hotel. Transcribe lo más claro posible."
            )

        texto = transcript.text.strip()
        print(f"📝 Transcripción: {texto}")
        return texto or "[Audio vacío]"
    except Exception as e:
        print("⚠️ Error al transcribir audio:", e)
        return "[Error al transcribir el audio]"
