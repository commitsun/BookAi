import os
import time
import logging
import requests
from typing import Optional

from langchain_openai import ChatOpenAI
from core.notification import notify_encargado
from core.memory_manager import MemoryManager
from core.language_manager import language_manager
from channels_wrapper.utils.text_utils import send_fragmented_async  # 👈 nuevo import

# ============================================================
# 🔧 CONFIGURACIÓN GLOBAL
# ============================================================

pending_escalations: dict[str, dict] = {}

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")
OPENAI_MODEL = "gpt-4.1-mini"

_global_memory = MemoryManager(max_runtime_messages=8)
log = logging.getLogger("escalation_manager")


# ============================================================
# 💬 ENVÍO DE MENSAJES A WHATSAPP (CON FRAGMENTACIÓN)
# ============================================================
async def send_whatsapp_text(user_id: str, text: str):
    """
    Envía texto al huésped por WhatsApp con fragmentación inteligente.
    Usa la API de Meta Graph.
    """
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_ID:
        log.error("❌ Falta WHATSAPP_TOKEN o WHATSAPP_PHONE_ID. No se puede enviar WhatsApp.")
        return

    async def _send_single(user_id_inner: str, body: str):
        url = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_ID}/messages"
        headers = {
            "Authorization": f"Bearer {WHATSAPP_TOKEN}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": user_id_inner,
            "type": "text",
            "text": {"body": body},
        }
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=15)
            log.info(f"📤 WhatsApp → {user_id_inner} (HTTP {r.status_code}) {body[:60]}...")
        except Exception as e:
            log.error(f"⚠️ Error enviando WhatsApp: {e}", exc_info=True)

    # Usa la función de fragmentación
    await send_fragmented_async(_send_single, user_id, text)


# ============================================================
# 🧠 DETECCIÓN DE IDIOMA DESDE MEMORIA
# ============================================================
def _extract_lang_from_history(conversation_id: str) -> Optional[str]:
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


# ============================================================
# 🚨 ESCALACIÓN HACIA ENCARGADO
# ============================================================
async def mark_pending(conversation_id: str, user_message: str):
    now = time.time()
    existing = pending_escalations.get(conversation_id)

    # Evitar duplicados recientes
    if existing and (now - existing.get("ts", 0)) < 15:
        log.info(f"⏭️ Escalación ya activa para {conversation_id}, evitando duplicados.")
        return

    pending_escalations[conversation_id] = {
        "question": user_message,
        "ts": now,
        "channel": "whatsapp",
    }

    lang = _extract_lang_from_history(conversation_id) or language_manager.detect_language(user_message)

    # Etiquetar idioma si falta
    try:
        tag = f"[lang:{lang}]"
        hist = _global_memory.get_context(conversation_id, limit=10)
        if not any(isinstance(m.get("content"), str) and m["content"].strip() == tag for m in hist):
            _global_memory.save(conversation_id, "system", tag)
    except Exception as e:
        log.warning(f"⚠️ No se pudo guardar tag de idioma: {e}")

    # 1️⃣ Mensaje al cliente
    phrase = "🕓 " + language_manager.short_phrase("Un momento por favor, voy a consultarlo con el encargado.", lang)
    await send_whatsapp_text(conversation_id, phrase)

    # 2️⃣ Aviso al encargado
    aviso = (
        f"📩 *Nueva consulta del cliente* (Idioma: {lang.upper()})\n"
        f"🆔 ID: `{conversation_id}`\n"
        f"❓ *Pregunta:* {user_message}\n\n"
        f"Responde con:\n`RESPUESTA {conversation_id}: <tu respuesta>`"
    )
    try:
        await notify_encargado(aviso)
    except Exception as e:
        log.error(f"❌ Error enviando aviso al encargado: {e}", exc_info=True)


# ============================================================
# 🧩 RESOLUCIÓN DESDE TELEGRAM
# ============================================================
async def resolve_from_encargado(conversation_id: str, raw_text: str, hybrid_agent):
    """
    Reformula el mensaje del encargado y lo reenvía al huésped.
    Aplica fragmentación, persistencia y limpieza de estado.
    """
    log.info(f"✉️ Resolviendo respuesta manual para {conversation_id}")

    original_msg = pending_escalations.get(conversation_id, {}).get("question", "")
    target_lang = _extract_lang_from_history(conversation_id) or language_manager.detect_language(original_msg or raw_text)

    llm = ChatOpenAI(model=OPENAI_MODEL, temperature=0.2)
    system_prompt = (
        "Responde SIEMPRE en el mismo idioma que el cliente.\n"
        "Reformula el texto del encargado con tono cálido y profesional.\n"
        "No menciones encargados, IA, ni procesos internos."
    )
    user_prompt = (
        f"Mensaje original del cliente:\n{original_msg}\n\n"
        f"Respuesta del encargado:\n{raw_text}\n\n"
        "Devuélveme solo el texto final para el cliente."
    )

    # 1️⃣ Reformular con LLM
    try:
        reformulated = await llm.ainvoke([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])
        final_text = reformulated.content.strip()
    except Exception as e:
        log.error(f"❌ Error al reformular: {e}", exc_info=True)
        final_text = raw_text.strip()

    # 2️⃣ Forzar idioma
    try:
        final_text = language_manager.ensure_language(final_text, target_lang)
    except Exception:
        pass

    # 3️⃣ Guardar memoria
    try:
        tag = f"[lang:{target_lang}]"
        hist = _global_memory.get_context(conversation_id, limit=10)
        if not any(isinstance(m.get("content"), str) and m["content"].strip() == tag for m in hist):
            _global_memory.save(conversation_id, "system", tag)
        _global_memory.save(conversation_id, "assistant", final_text)
    except Exception as e:
        log.warning(f"⚠️ No se pudo guardar memoria: {e}")

    # 4️⃣ Enviar con fragmentación
    await send_whatsapp_text(conversation_id, final_text)

    # 5️⃣ Limpiar estado
    pending_escalations.pop(conversation_id, None)

    # 6️⃣ Confirmar al encargado
    try:
        await notify_encargado(
            f"✅ Respuesta enviada al cliente `{conversation_id}`.\n\n🧾 *Mensaje final:* {final_text}"
        )
    except Exception as e:
        log.error(f"⚠️ No se pudo confirmar al encargado: {e}", exc_info=True)
