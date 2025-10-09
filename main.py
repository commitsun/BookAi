# =====================================================
# 🏨 HotelAI — Orquestador con Escalación en Pausa
# =====================================================
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import asyncio
import aiohttp
import logging
import os
import time
import requests

from channels_wrapper.manager import ChannelManager
from core.main_agent import HotelAIHybrid
from channels_wrapper.telegram.telegram_channel import register_routes as register_telegram_channel

# --- Entorno WhatsApp / Telegram ---
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_ENCARGADO_CHAT_ID = os.getenv("TELEGRAM_ENCARGADO_CHAT_ID")

# =====================================================
# 🚀 FastAPI
# =====================================================
app = FastAPI(title="HotelAI - Multi-Channel Hybrid Bot")
logging.basicConfig(level=logging.INFO)

# =====================================================
# 🧠 Agente híbrido
# =====================================================
hybrid_agent = HotelAIHybrid()

# =====================================================
# 🗂️ Pendientes de escalación (memoria en proceso)
#  conversation_id -> {"question": str, "ts": float, "channel": "whatsapp"}
# =====================================================
pending_escalations: dict[str, dict] = {}

# =====================================================
# 📣 Notificador global al encargado (Telegram)
# =====================================================
async def notify_encargado(mensaje: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_ENCARGADO_CHAT_ID:
        logging.warning("⚠️ Variables Telegram no configuradas.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_ENCARGADO_CHAT_ID, "text": mensaje}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=data, timeout=15) as resp:
                logging.info(f"📨 Aviso al encargado (HTTP {resp.status})")
    except Exception as e:
        logging.error(f"❌ Error enviando aviso a Telegram: {e}", exc_info=True)

# =====================================================
# 📝 Marcar conversación como pendiente (no respondemos al huésped)
# =====================================================
async def mark_pending(conversation_id: str, user_message: str):
    pending_escalations[conversation_id] = {
        "question": user_message,
        "ts": time.time(),
        "channel": "whatsapp",
    }
    aviso = (
        f"📩 El cliente {conversation_id} preguntó:\n{user_message}\n\n"
        "✍️ Responde con el formato:\n"
        "RESPUESTA {ID_SIN_MAS}: tu texto aquí\n\n"
        "Ejemplo:\nRESPUESTA 34600000000: Sí, tenemos cuna disponible y es gratuita."
    )
    await notify_encargado(aviso)

# =====================================================
# 🔁 Resolver respuesta del encargado → formatear → enviar al huésped
# =====================================================
async def resolve_from_encargado(conversation_id: str, raw_text: str):
    if conversation_id not in pending_escalations:
        # No está pendiente, igual lo reenvíamos directamente
        logging.info(f"ℹ️ {conversation_id} no estaba pendiente. Reenviando igualmente.")
    # Reformatear con el agente (tono del hotel, mismo idioma del cliente)
    try:
        # Le pasamos como prompt el texto del encargado para pulirlo
        formatted = await hybrid_agent.process_message(
            user_message=raw_text,
            conversation_id=conversation_id
        )
    except Exception:
        formatted = raw_text  # fallback

    # Enviar al huésped por WhatsApp
    send_whatsapp_text(conversation_id, formatted)

    # Cerrar pendiente (si existía)
    pending_escalations.pop(conversation_id, None)
    logging.info(f"✅ Conversación {conversation_id} resuelta y enviada al huésped.")

# =====================================================
# ✉️ Envío WhatsApp “raw” (para usar fuera del canal)
# =====================================================
def send_whatsapp_text(user_id: str, text: str):
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_ID:
        logging.error("❌ Falta WHATSAPP_TOKEN o WHATSAPP_PHONE_ID.")
        return
    url = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": user_id,
        "type": "text",
        "text": {"body": text},
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=15)
        logging.info(f"📤 WhatsApp → {user_id} (HTTP {r.status_code})")
    except Exception as e:
        logging.error(f"⚠️ Error enviando WhatsApp: {e}", exc_info=True)

# =====================================================
# 🔌 Canales
# =====================================================
manager = ChannelManager()
for name, channel in manager.channels.items():
    channel.agent = hybrid_agent
    channel.register_routes(app)
    logging.info(f"✅ Canal '{name}' registrado y conectado al agente.")

# Canal interno del encargado (Telegram)
register_telegram_channel(app)
logging.info("✅ Canal interno Telegram (encargado) registrado.")

# =====================================================
# 🩺 Salud
# =====================================================
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "channels": list(manager.channels.keys()) + ["telegram_encargado"],
        "pending": list(pending_escalations.keys())
    }

# =====================================================
# 💬 Endpoint genérico
# =====================================================
@app.post("/api/message")
async def api_message(request: Request):
    try:
        data = await request.json()
        user_message = data.get("message", "").strip()
        conversation_id = str(data.get("conversation_id", "unknown")).replace("+", "").strip()
        if not user_message:
            return JSONResponse({"error": "Mensaje vacío"}, status_code=400)

        # Procesar con el agente
        response = await hybrid_agent.process_message(user_message, conversation_id)

        # ¿Debemos escalar en pausa? (mismas reglas que WhatsApp)
        if any(p in response.lower() for p in [
            "contactar con el encargado",
            "no dispongo",
            "permíteme contactar",
            "he contactado con el encargado",
            "error",
        ]):
            await mark_pending(conversation_id, user_message)
            # No devolvemos respuesta “al cliente”; aquí solo informamos a quien consume la API
            return JSONResponse({"response": "🕓 Consultando con el encargado..."})

        return JSONResponse({"response": response})
    except Exception as e:
        logging.error(f"⚠️ Error en /api/message: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

# =====================================================
# 🧩 Hooks que usarán los canales
# =====================================================
# Disponibles para import desde otros módulos:
# - mark_pending(conversation_id, user_message)
# - resolve_from_encargado(conversation_id, raw_text)
# - notify_encargado(mensaje)
# - send_whatsapp_text(user_id, text)
