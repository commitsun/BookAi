import os
import time
import logging
import requests
from core.notification import notify_encargado
from core.memory_manager import MemoryManager

pending_escalations: dict[str, dict] = {}

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")

# 🔒 Memoria global para consolidar la “verdad” tras la respuesta del encargado
_global_memory = MemoryManager(max_runtime_messages=8)

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

# --- utilidad simple de idioma para el aviso inicial (sin LLM) ---
def _guess_lang(text: str) -> str:
    t = (text or "").strip().lower()
    if not t:
        return "es"
    # señales rápidas (sin dependencias)
    if any(w in t for w in ["the ", "is ", "do ", "can ", "near", "around", "hello", "hi "]):
        return "en"
    if any(w in t for w in ["bonjour", "s'il", "où", "ou ", "merci"]):
        return "fr"
    if any(w in t for w in ["ciao", "per favore", "dove", "grazie"]):
        return "it"
    if any(w in t for w in ["olá", "ola ", "por favor", "onde", "obrigado", "obrigada"]):
        return "pt"
    if any(w in t for w in ["hallo", "bitte", "wo ", "danke"]):
        return "de"
    if "¿" in t or "¡" in t or any(w in t for w in ["por favor", "hola", "gracias", "dónde", "donde"]):
        return "es"
    return "es"

def _escalate_phrase(lang: str) -> str:
    # Emoji SIEMPRE al inicio
    mapping = {
        "es": "🕓 Un momento por favor, voy a consultarlo con el encargado.",
        "en": "🕓 One moment please, I’m going to check this with the manager.",
        "fr": "🕓 Un instant s’il vous plaît, je vais le consulter avec le responsable.",
        "it": "🕓 Un momento per favore, lo verificherò con il responsabile.",
        "pt": "🕓 Um momento por favor, vou verificar isso com o responsável.",
        "de": "🕓 Einen Moment bitte, ich kläre das mit dem Verantwortlichen.",
    }
    return mapping.get(lang, mapping["es"])

async def mark_pending(conversation_id: str, user_message: str):
    """Marca conversación como pendiente, avisa al cliente y notifica al encargado."""
    pending_escalations[conversation_id] = {
        "question": user_message,
        "ts": time.time(),
        "channel": "whatsapp",
    }

    # 🕓 Aviso al cliente (multi-idioma sencillo)
    lang = _guess_lang(user_message)
    send_whatsapp_text(conversation_id, _escalate_phrase(lang))

    # 📨 Aviso al encargado
    aviso = (
        f"📩 *El cliente {conversation_id} preguntó:*\n"
        f"“{user_message}”\n\n"
        "✉️ Escribe tu respuesta directamente aquí y el sistema la enviará al cliente."
    )
    await notify_encargado(aviso)

async def resolve_from_encargado(conversation_id: str, raw_text: str, hybrid_agent):
    """
    Reformula la respuesta dada por el encargado y la envía al cliente
    en el idioma en que el cliente habló originalmente. No menciona procesos internos.
    """
    import logging
    from langchain_openai import ChatOpenAI

    logging.info(f"✉️ Resolviendo respuesta manual para {conversation_id}")

    original_user_message = pending_escalations.get(conversation_id, {}).get("question", "")

    # LLM directo para reformular (sin routing/tools)
    llm = ChatOpenAI(model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"), temperature=0.2)

    system_prompt = (
        "Responde SIEMPRE en el MISMO idioma que el siguiente mensaje del cliente.\n"
        "Reformula el texto del encargado para el cliente con un tono cálido, claro y profesional.\n"
        "No menciones procesos internos, ni que proviene de un encargado, ni IA.\n"
        "Sé conciso (2–4 frases) y evita muletillas o cierres largos."
    )
    user_prompt = (
        f"Mensaje original del cliente (para detectar idioma):\n{original_user_message}\n\n"
        f"Respuesta del encargado (posiblemente en otro idioma):\n{raw_text}\n\n"
        "Devuélveme únicamente el mensaje final para el cliente."
    )

    try:
        reformulated = await llm.ainvoke([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ])
        final_text = reformulated.content.strip()
    except Exception as e:
        logging.error(f"❌ Error al reformular respuesta del encargado: {e}", exc_info=True)
        final_text = raw_text

    # Guardar en memoria como “verdad” oficial
    try:
        _global_memory.save(conversation_id, "assistant", final_text)
        logging.info(f"🧠 Memoria actualizada (encargado) para {conversation_id}: {final_text}")
    except Exception as e:
        logging.error(f"⚠️ No se pudo guardar en memoria: {e}")

    # Enviar al huésped
    send_whatsapp_text(conversation_id, final_text)
    pending_escalations.pop(conversation_id, None)
    logging.info(f"✅ Conversación {conversation_id} resuelta y enviada al cliente.")

    # Confirmación al encargado
    await notify_encargado(
        f"✅ Respuesta enviada al cliente *{conversation_id}*.\n\n🧾 *Mensaje final:* {final_text}"
    )
