# =====================================================
# 🏨 HotelAI — Orquestador con Escalación Elegante
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

# =====================================================
# 🌍 Entorno
# =====================================================
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
# 🧠 Agente híbrido principal
# =====================================================
hybrid_agent = HotelAIHybrid()

# =====================================================
# 🗂️ Pendientes de escalación (en memoria temporal)
# =====================================================
pending_escalations: dict[str, dict] = {}

# =====================================================
# 📣 Notificar al encargado (Telegram)
# =====================================================
async def notify_encargado(mensaje: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_ENCARGADO_CHAT_ID:
        logging.warning("⚠️ Variables Telegram no configuradas.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_ENCARGADO_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=data, timeout=15) as resp:
                logging.info(f"📨 Aviso al encargado (HTTP {resp.status})")
    except Exception as e:
        logging.error(f"❌ Error enviando aviso a Telegram: {e}", exc_info=True)

# =====================================================
# ✉️ Envío WhatsApp “raw”
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
# 📝 Marcar conversación como pendiente
# =====================================================
async def mark_pending(conversation_id: str, user_message: str):
    """Marca conversación como pendiente, avisa al cliente y notifica al encargado."""
    pending_escalations[conversation_id] = {
        "question": user_message,
        "ts": time.time(),
        "channel": "whatsapp",
    }

    # 🕓 Avisar al cliente
    send_whatsapp_text(
        conversation_id,
        "🕓 Estamos consultando esta información con el encargado del hotel. "
        "Te responderemos en unos minutos. Gracias por tu paciencia."
    )

    # 📢 Avisar al encargado
    aviso = (
        f"📩 *El cliente {conversation_id} preguntó:*\n"
        f"“{user_message}”\n\n"
        "✉️ Escribe tu respuesta directamente aquí y el sistema la enviará al cliente."
    )
    await notify_encargado(aviso)

# =====================================================
# 🔁 Resolver respuesta del encargado → formatear → enviar
# =====================================================
async def resolve_from_encargado(conversation_id: str, raw_text: str):
    """Procesa la respuesta del encargado, la reformatea y la envía al huésped."""
    logging.info(f"✉️ Resolviendo respuesta manual para {conversation_id}")

    if conversation_id not in pending_escalations:
        await notify_encargado("⚠️ No había conversación pendiente, pero la respuesta se enviará igualmente.")

    try:
        # Reformatear con tono cálido y profesional
        formatted = await hybrid_agent.process_message(
            f"El encargado del hotel responde al cliente con este texto:\n\n{raw_text}\n\n"
            f"Reformula la respuesta con tono amable, profesional y natural, "
            f"sin alterar el contenido original."
        )
    except Exception as e:
        logging.error(f"❌ Error al reformatear respuesta: {e}")
        formatted = raw_text

    # 📤 Enviar al huésped
    send_whatsapp_text(conversation_id, formatted)

    # 🧹 Limpiar pendientes
    pending_escalations.pop(conversation_id, None)
    logging.info(f"✅ Conversación {conversation_id} resuelta y enviada.")

    # ✅ Confirmar al encargado
    confirmacion = (
        f"✅ Tu respuesta fue enviada correctamente al cliente *{conversation_id}*.\n\n"
        f"🧾 *Mensaje final enviado:*\n{formatted}"
    )
    await notify_encargado(confirmacion)

# =====================================================
# 🔌 Registro de canales
# =====================================================
manager = ChannelManager()
for name, channel in manager.channels.items():
    channel.agent = hybrid_agent
    channel.register_routes(app)
    logging.info(f"✅ Canal '{name}' registrado y conectado al agente.")

# Canal interno (Telegram encargado)
register_telegram_channel(app)
logging.info("✅ Canal interno Telegram registrado.")

# =====================================================
# 🩺 Healthcheck
# =====================================================
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "channels": list(manager.channels.keys()) + ["telegram_encargado"],
        "pending": list(pending_escalations.keys())
    }

# =====================================================
# 💬 Endpoint API genérico
# =====================================================
@app.post("/api/message")
async def api_message(request: Request):
    try:
        data = await request.json()
        user_message = data.get("message", "").strip()
        conversation_id = str(data.get("conversation_id", "unknown")).replace("+", "").strip()
        if not user_message:
            return JSONResponse({"error": "Mensaje vacío"}, status_code=400)

        response = await hybrid_agent.process_message(user_message, conversation_id)

        # Detección de necesidad de escalación
        if any(p in response.lower() for p in [
            "contactar con el encargado",
            "no dispongo",
            "permíteme contactar",
            "he contactado con el encargado",
            "error",
        ]):
            await mark_pending(conversation_id, user_message)
            return JSONResponse({"response": "🕓 Consultando con el encargado..."})

        return JSONResponse({"response": response})
    except Exception as e:
        logging.error(f"⚠️ Error en /api/message: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)
