import json
import logging
import re
from core.language import enforce_language

_last_reply_cache = {}

def normalize_reply(raw_reply, user_question, language=None, source="Unknown") -> str:
    """
    Limpia y normaliza respuestas crudas (strings, dicts, listas o JSON anidados),
    extrayendo contenido legible y eliminando ruido estructural.
    """
    global _last_reply_cache

    def extract_page_content(text):
        """Extrae el contenido humano desde un JSON anidado o texto crudo."""
        try:
            if isinstance(text, str):
                # Intentar decodificar si parece JSON
                if text.strip().startswith("{") and "pageContent" in text:
                    data = json.loads(text)
                    return data.get("pageContent", text)
                # Si hay secuencias escapadas
                cleaned = re.sub(r"\\[nrt]", " ", text)
                cleaned = re.sub(r"\s{2,}", " ", cleaned)
                return cleaned.strip()
            elif isinstance(text, dict):
                return text.get("pageContent") or text.get("text") or str(text)
            else:
                return str(text)
        except Exception as e:
            logging.warning(f"⚠️ Error extrayendo contenido: {e}")
            return str(text)

    # ---- 1️⃣ Unificar la respuesta según tipo ----
    if isinstance(raw_reply, list):
        parts = [extract_page_content(item) for item in raw_reply]
        reply = "\n\n".join(p for p in parts if p and isinstance(p, str))
    elif isinstance(raw_reply, dict):
        reply = extract_page_content(raw_reply)
    else:
        reply = extract_page_content(raw_reply)

    # ---- 2️⃣ Limpiar formato visual ----
    reply = reply.replace("\\n", "\n").replace("\\t", " ").replace("\\", "")
    reply = re.sub(r"\n{3,}", "\n\n", reply).strip()

    # ---- 3️⃣ Evitar duplicados ----
    cache_key = source
    if _last_reply_cache.get(cache_key) == reply:
        return "No dispongo de ese dato en este momento."
    _last_reply_cache[cache_key] = reply

    # ---- 4️⃣ Detectar respuestas finales ----
    lower_reply = reply.lower()
    final_markers = [
        "✅", "disponibilidad del", "reserva confirmada", "🏨", "€/noche"
    ]
    if any(marker in lower_reply for marker in final_markers):
        logging.info(f"🟢 normalize_reply: respuesta final detectada desde {source}, no se reescribe.")
        return reply

    # ---- 5️⃣ Aplicar reescritura controlada ----
    try:
        final_reply = enforce_language(user_question, reply, language)
        return final_reply
    except Exception as e:
        logging.error(f"⚠️ Error aplicando enforce_language: {e}")
        return reply
