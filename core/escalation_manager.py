# core/escalation_manager.py
import os
import time
import logging
import requests
from core.notification import notify_encargado

pending_escalations: dict[str, dict] = {}

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")


def send_whatsapp_text(user_id: str, text: str):
    """Envía un mensaje de texto básico a WhatsApp usando la API de Meta."""
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


async def resolve_from_encargado(conversation_id: str, raw_text: str, hybrid_agent):
    """Procesa la respuesta del encargado, la reformatea y la envía al huésped."""
    logging.info(f"✉️ Resolviendo respuesta manual para {conversation_id}")

    if conversation_id not in pending_escalations:
        await notify_encargado("⚠️ No había conversación pendiente, pero la respuesta se enviará igualmente.")

    try:
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
    pending_escalations.pop(conversation_id, None)
    logging.info(f"✅ Conversación {conversation_id} resuelta y enviada.")

    # ✅ Confirmar al encargado
    confirmacion = (
        f"✅ Tu respuesta fue enviada correctamente al cliente *{conversation_id}*.\n\n"
        f"🧾 *Mensaje final enviado:*\n{formatted}"
    )
    await notify_encargado(confirmacion)
