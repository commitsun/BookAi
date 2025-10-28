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

# 🧩 Conversaciones en las que estamos esperando respuesta humana
pending_escalations: dict[str, dict] = {}

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")

# 🧠 Hardcodeamos el modelo para evitar error 400 (model parameter missing)
OPENAI_MODEL = "gpt-4.1-mini"

# Memoria compartida y persistente
_global_memory = MemoryManager(max_runtime_messages=8)


def send_whatsapp_text(user_id: str, text: str):
    """
    Envía texto al huésped por WhatsApp usando la API de Meta.
    Si falta config de WhatsApp, lo loguea y no revienta el flujo.
    """
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_ID:
        logging.error("❌ Falta WHATSAPP_TOKEN o WHATSAPP_PHONE_ID. No se puede enviar WhatsApp.")
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
    """
    Recupera [lang:xx] del historial persistente.
    Si no existe, devuelve None.
    """
    try:
        history = _global_memory.get_context(conversation_id, limit=20)
        for msg in reversed(history):
            content = (msg or {}).get("content", "")
            if (
                isinstance(content, str)
                and content.strip().startswith("[lang:")
                and content.strip().endswith("]")
            ):
                inner = content.strip()[6:-1].lower()
                if len(inner) == 2:
                    return inner
        return None
    except Exception:
        return None


async def mark_pending(conversation_id: str, user_message: str):
    """
    Marca la conversación como pendiente de respuesta humana.
    1. Guarda estado en pending_escalations.
    2. Envía mensaje corto al huésped: "🕓 Un momento por favor..."
    3. Envía aviso al encargado por Telegram con el ID del cliente.
    IMPORTANTE: si Telegram no está configurado, NO revienta el flujo.
    """

    now = time.time()
    existing = pending_escalations.get(conversation_id)

    # Evitar duplicar una escalación recién creada
    if existing and (now - existing.get("ts", 0)) < 15:
        logging.info(f"⏭️ Escalación ya activa para {conversation_id}, evitando duplicados.")
    else:
        pending_escalations[conversation_id] = {
            "question": user_message,
            "ts": now,
            "channel": "whatsapp",
        }

    # Idioma del huésped
    lang = _extract_lang_from_history(conversation_id) or language_manager.detect_language(user_message)

    # Etiquetar idioma en memoria si aún no estaba
    try:
        tag = f"[lang:{lang}]"
        history = _global_memory.get_context(conversation_id, limit=10)
        if not any(
            isinstance(m.get("content"), str) and m["content"].strip() == tag
            for m in history
        ):
            _global_memory.save(conversation_id, "system", tag)
    except Exception as e:
        logging.warning(f"⚠️ No se pudo guardar tag de idioma en mark_pending: {e}")

    # 1. Aviso visible al cliente
    base_meaning_es = "Un momento por favor, voy a consultarlo con el encargado."
    phrase = "🕓 " + language_manager.short_phrase(base_meaning_es, lang)
    send_whatsapp_text(conversation_id, phrase)

    # 2. Aviso interno al encargado (Telegram)
    lang_label = lang.upper()
    aviso_encargado = (
        f"📩 *Nueva consulta del cliente* (Idioma: {lang_label})\n"
        f"🆔 ID: `{conversation_id}`\n"
        f"❓ *Pregunta:* {user_message}\n\n"
        f"Responde con:\n"
        f"`RESPUESTA {conversation_id}: <tu respuesta>`"
    )

    try:
        ok = await notify_encargado(aviso_encargado)
        logging.info("📨 Aviso enviado al encargado (o ignorado si no hay Telegram).")
    except Exception as e:
        logging.error(f"❌ Error enviando aviso al encargado: {e}", exc_info=True)


async def resolve_from_encargado(conversation_id: str, raw_text: str, hybrid_agent):
    """
    El encargado ya ha contestado por Telegram.
    Esta función:
      - Reformula su mensaje (tono cálido, claro, sin procesos internos).
      - Garantiza idioma del cliente.
      - Envía WhatsApp al cliente.
      - Limpia la escalación pendiente.
      - Avisa al encargado de que se envió.

    Si algo falla, hacemos fallback y mandamos el texto tal cual.
    """

    logging.info(f"✉️ Resolviendo respuesta manual para {conversation_id}")

    original_user_message = pending_escalations.get(conversation_id, {}).get("question", "")

    # Idioma real que debemos usar con ese cliente
    target_lang = (
        _extract_lang_from_history(conversation_id)
        or language_manager.detect_language(original_user_message or raw_text)
    )

    # ✅ Se usa modelo hardcodeado para evitar error "model parameter missing"
    llm = ChatOpenAI(model=OPENAI_MODEL, temperature=0.2)

    system_prompt = (
        "Responde SIEMPRE en el MISMO idioma que el siguiente mensaje del cliente.\n"
        "Reformula el texto del encargado para el cliente con un tono cálido, claro y profesional.\n"
        "No menciones procesos internos, ni que proviene de un encargado, ni IA.\n"
        "Sé conciso (2–4 frases) y evita muletillas o cierres largos.\n"
        "No uses frases tipo 'estoy aquí para ayudarte' ni cierres promocionales tipo 'te esperamos'."
    )

    user_prompt = (
        f"Mensaje original del cliente (para detectar idioma):\n{original_user_message}\n\n"
        f"Respuesta del encargado (posiblemente en otro idioma):\n{raw_text}\n\n"
        "Devuélveme únicamente el mensaje final para el cliente."
    )

    # 1. Reformular con el LLM
    try:
        reformulated = await llm.ainvoke(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
        )
        final_text = reformulated.content.strip()
    except Exception as e:
        logging.error(f"❌ Error al reformular respuesta del encargado: {e}", exc_info=True)
        final_text = raw_text.strip()

    # 2. Forzar idioma correcto
    try:
        final_text = language_manager.ensure_language(final_text, target_lang)
    except Exception:
        pass

    # 3. Guardar memoria
    try:
        tag = f"[lang:{target_lang}]"
        hist = _global_memory.get_context(conversation_id, limit=10)
        if not any(
            isinstance(m.get("content"), str) and m["content"].strip() == tag
            for m in hist
        ):
            _global_memory.save(conversation_id, "system", tag)

        _global_memory.save(conversation_id, "assistant", final_text)
        logging.info(f"🧠 Memoria actualizada (encargado) para {conversation_id}: {final_text}")
    except Exception as e:
        logging.error(f"⚠️ No se pudo guardar en memoria: {e}", exc_info=True)

    # 4. Enviar WhatsApp al huésped
    send_whatsapp_text(conversation_id, final_text)

    # 5. Limpiar marca de pendiente
    if conversation_id in pending_escalations:
        pending_escalations.pop(conversation_id, None)

    # 6. Avisar al encargado que se mandó correctamente
    try:
        ack_msg = (
            f"✅ Respuesta enviada al cliente `{conversation_id}`.\n\n"
            f"🧾 Mensaje final:\n{final_text}"
        )
        await notify_encargado(ack_msg)
    except Exception as e:
        logging.error(f"⚠️ No se pudo confirmar al encargado: {e}", exc_info=True)
