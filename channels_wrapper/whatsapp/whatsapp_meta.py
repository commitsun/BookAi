import os
import requests
import json
import re
import time
import random
import asyncio
from io import BytesIO
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from core.graph import app as bot_app
from openai import OpenAI

# --- Configuración ---
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "midemo")

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
fastapi_app = FastAPI()
conversations = {}
processed_ids = set()

# ------------------------------------------------------------------
# Fragmentar texto naturalmente
# ------------------------------------------------------------------
def fragment_text_intelligently(text: str) -> list[str]:
    text = re.sub(r'\n{2,}', '\n', text.strip())
    raw_parts = re.split(r'(?:(?<=\n)\d+\.|\n-|\n•|\n(?=[A-Z]))', text)
    fragments, buffer = [], ""

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
        merged, temp = [], ""
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
# Enviar mensajes con simulación de escritura
# ------------------------------------------------------------------
def send_message(to: str, text: str):
    url = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    fragments = fragment_text_intelligently(text)

    for i, frag in enumerate(fragments):
        requests.post(url, headers=headers, json={"messaging_product": "whatsapp", "to": to, "type": "typing_on"})
        time.sleep(random.uniform(1.5, 3.5) if len(frag) < 80 else random.uniform(3.0, 5.0))
        r = requests.post(url, headers=headers, json={
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": frag}
        })
        print(f"📤 Enviado fragmento {i+1}/{len(fragments)} ({r.status_code})")
        requests.post(url, headers=headers, json={"messaging_product": "whatsapp", "to": to, "type": "typing_off"})
        time.sleep(random.uniform(0.5, 1.5))


# ------------------------------------------------------------------
# --- Webhook de verificación ---
# ------------------------------------------------------------------
@fastapi_app.get("/webhook")
async def verify_webhook(request: Request):
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(params.get("hub.challenge"), status_code=200)
    return PlainTextResponse("Error de verificación", status_code=403)


# ------------------------------------------------------------------
# --- Webhook principal ---
# ------------------------------------------------------------------
@fastapi_app.post("/webhook")
async def whatsapp_webhook(request: Request):
    data = await request.json()
    print("📩 Payload recibido:\n", json.dumps(data, indent=2, ensure_ascii=False))
    asyncio.create_task(procesar_mensaje_async(data))
    return JSONResponse({"status": "ok"})


# ------------------------------------------------------------------
# --- Procesamiento del mensaje ---
# ------------------------------------------------------------------
async def procesar_mensaje_async(data: dict):
    global processed_ids
    try:
        entry = data.get("entry", [])[0]["changes"][0]["value"]
        if "messages" not in entry:
            return

        msg = entry["messages"][0]
        msg_type = msg.get("type")
        user_id = msg["from"]
        msg_id = msg.get("id")

        if msg_id in processed_ids:
            print(f"🔁 Mensaje duplicado ignorado: {msg_id}")
            return

        processed_ids.add(msg_id)
        if len(processed_ids) > 5000:
            processed_ids = set(list(processed_ids)[-2000:])

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
            user_msg = msg["image"].get("caption", "El cliente envió una imagen.")
        elif msg_type == "audio":
            media_id = msg["audio"]["id"]
            user_msg = transcribir_audio(media_id)
        else:
            user_msg = f"[Mensaje tipo {msg_type} no soportado]"

        conversations[user_id].append({"role": "user", "content": user_msg})

        state = {"messages": conversations[user_id]}
        state = await bot_app.ainvoke(state)
        reply = state["messages"][-1]["content"]

        conversations[user_id].append({"role": "assistant", "content": reply})
        send_message(user_id, reply)

    except Exception as e:
        print("⚠️ Error procesando mensaje:", e)


# ------------------------------------------------------------------
# --- Funciones auxiliares ---
# ------------------------------------------------------------------
def download_media_bytes(media_id: str):
    """Descarga contenido multimedia y lo devuelve en memoria (BytesIO)."""
    try:
        url = f"https://graph.facebook.com/v19.0/{media_id}"
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
        r = requests.get(url, headers=headers)
        if r.status_code != 200:
            print(f"❌ Error obteniendo URL de media {media_id}: {r.text}")
            return None
        media_url = r.json().get("url")
        if not media_url:
            print("❌ No se encontró URL de media.")
            return None
        r = requests.get(media_url, headers=headers)
        if r.status_code == 200:
            return BytesIO(r.content)
        print(f"❌ Error descargando media {media_id}: {r.text}")
        return None
    except Exception as e:
        print("⚠️ Error al descargar media:", e)
        return None


def transcribir_audio(media_id: str) -> str:
    """Descarga y transcribe un audio usando Whisper sin guardar archivos."""
    try:
        audio_bytes = download_media_bytes(media_id)
        if not audio_bytes:
            return "[Error: no se pudo descargar el audio]"
        transcript = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_bytes,
            prompt="Pregunta de un cliente sobre un hotel. Transcribe lo más claro posible."
        )
        texto = transcript.text.strip()
        print(f"📝 Transcripción: {texto}")
        return texto or "[Audio vacío]"
    except Exception as e:
        print("⚠️ Error al transcribir audio:", e)
        return "[Error al transcribir el audio]"
