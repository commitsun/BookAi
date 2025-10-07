import json
from core.language import enforce_language

# 🔹 Caché global para recordar la última respuesta enviada
_last_reply_cache = {}

def normalize_reply(raw_reply, user_question, language=None, source="InfoAgent") -> str:
    """
    Convierte la respuesta cruda en texto limpio, elimina duplicados innecesarios
    y fuerza idioma de salida.
    """
    global _last_reply_cache

    try:
        # ---- Normalización del raw_reply ----
        if isinstance(raw_reply, str):
            try:
                data = json.loads(raw_reply)
                if isinstance(data, dict) and "pageContent" in data:
                    reply = data["pageContent"]
                else:
                    reply = raw_reply
            except Exception:
                reply = raw_reply

        elif isinstance(raw_reply, dict):
            reply = (
                raw_reply.get("respuesta")
                or raw_reply.get("pageContent")
                or raw_reply.get("text")
                or json.dumps(raw_reply, ensure_ascii=False)
            )

        elif isinstance(raw_reply, list):
            # Nos quedamos con el primer texto útil de la lista
            reply = None
            for item in raw_reply:
                if isinstance(item, dict):
                    reply = item.get("pageContent") or item.get("text")
                    if reply:
                        break
                elif isinstance(item, str):
                    reply = item
                    break
            if not reply:
                reply = str(raw_reply)

        else:
            reply = str(raw_reply)

    except Exception as e:
        reply = f"⚠️ Error procesando respuesta de {source}: {e}"

    # ---- Evitar duplicados exactos, excepto si es la frase estándar ----
    reply_clean = reply.strip()
    cache_key = f"{source}"

    last = _last_reply_cache.get(cache_key)

    # Solo bloqueamos si es exactamente igual Y no es la frase estándar
    if (
        last == reply_clean
        and "No dispongo de ese dato" not in reply_clean
    ):
        return "No dispongo de ese dato en este momento."

    # Actualizar caché con la última respuesta
    _last_reply_cache[cache_key] = reply_clean

    # ---- Forzar idioma correcto ----
    final_reply = enforce_language(user_question, reply_clean, language)

    return final_reply
 