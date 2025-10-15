# core/escalation_manager.py
import os
import time
import logging
from typing import Optional

import requests
from langchain_openai import ChatOpenAI

from core.notification import notify_encargado
from core.memory_manager import MemoryManager
from core.language_manager import language_manager

pending_escalations: dict[str, dict] = {}

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

# Memoria para consolidar verdad tras respuesta del encargado
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


def _extract_lang_from_history(conversation_id: str) -> Optional[str]:
    """Recupera [lang:xx] del historial persistente (cualquier role)."""
    try:
        history = _global_memory.get_context(conversation_id, limit=20)
        for msg in reversed(history):
            content = (msg or {}).get("content", "")
            if isinstance(content, str) and content.strip().startswith("[lang:") and content.strip().endswith("]"):
                inner = content.strip()[6:-1].lower()
                if len(inner) == 2:
                    return inner
        return None
    except Exception:
        return None


async def mark_pending(conversation_id: str, user_message: str):
    """Marca conversación como pendiente, avisa al cliente y notifica al encargado."""
    now = time.time()
    existing = pending_escalations.get(conversation_id)

    # Evitar duplicados si ya se escaló hace muy poco
    if existing and (now - existing.get("ts", 0)) < 15:
        logging.info(f"⏭️ Escalación ya activa para {conversation_id}, evitando duplicados.")
        return

    pending_escalations[conversation_id] = {
        "question": user_message,
        "ts": now,
        "channel": "whatsapp",
    }

    # Idioma del cliente: historial → detección
    lang = _extract_lang_from_history(conversation_id) or language_manager.detect_language(user_message)

    # Persistir tag de idioma si no existía (role='system' para evitar constraint)
    try:
        tag = f"[lang:{lang}]"
        history = _global_memory.get_context(conversation_id, limit=10)
        if not any(isinstance(m.get("content"), str) and m["content"].strip() == tag for m in history):
            _global_memory.save(conversation_id, "system", tag)
    except Exception as e:
        logging.warning(f"⚠️ No se pudo guardar tag de idioma en mark_pending: {e}")

    # Aviso breve al cliente (traducido dinámicamente)
    base_meaning_es = "Un momento por favor, voy a consultarlo con el encargado."
    phrase = "🕓 " + language_manager.short_phrase(base_meaning_es, lang)
    send_whatsapp_text(conversation_id, phrase)

    # Aviso al encargado (incluye idioma del cliente)
    lang_label = lang.upper()
    aviso = (
        f"📩 *Nueva consulta del cliente* (Idioma: {lang_label})\n"
        f"🆔 ID: `{conversation_id}`\n"
        f"❓ *Pregunta:* {user_message}\n\n"
        f"Responde con:\n"
        f"`RESPUESTA {conversation_id}: <tu respuesta>`"
    )
    await notify_encargado(aviso)


async def resolve_from_encargado(conversation_id: str, raw_text: str, hybrid_agent):
    """
    Reformula la respuesta del encargado y la envía al cliente
    en el idioma del cliente. No menciona procesos internos.
    """
    logging.info(f"✉️ Resolviendo respuesta manual para {conversation_id}")

    original_user_message = pending_escalations.get(conversation_id, {}).get("question", "")

    # Idioma objetivo: historial o detectado
    target_lang = _extract_lang_from_history(conversation_id) or language_manager.detect_language(original_user_message or raw_text)

    llm = ChatOpenAI(model=OPENAI_MODEL, temperature=0.2)

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

    # Garantizar idioma destino exacto
    try:
        final_text = language_manager.ensure_language(final_text, target_lang)
    except Exception:
        pass

    # Persistencia y envío
    try:
        tag = f"[lang:{target_lang}]"
        hist = _global_memory.get_context(conversation_id, limit=10)
        if not any(isinstance(m.get("content"), str) and m["content"].strip() == tag for m in hist):
            _global_memory.save(conversation_id, "system", tag)

        _global_memory.save(conversation_id, "assistant", final_text)
        logging.info(f"🧠 Memoria actualizada (encargado) para {conversation_id}: {final_text}")
    except Exception as e:
        logging.error(f"⚠️ No se pudo guardar en memoria: {e}")

    send_whatsapp_text(conversation_id, final_text)
    pending_escalations.pop(conversation_id, None)

    await notify_encargado(
        f"✅ Respuesta enviada al cliente *{conversation_id}*.\n\n🧾 *Mensaje final:* {final_text}"
    )
