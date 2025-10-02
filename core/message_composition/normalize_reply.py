import json
from core.language import enforce_language

def normalize_reply(raw_reply, user_question, language=None, source="Agent") -> str:
    """
    Convierte la respuesta cruda en texto limpio y forzado al idioma correcto.
    """
    try:
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
            reply = raw_reply.get("respuesta") or raw_reply.get("pageContent") or json.dumps(raw_reply, ensure_ascii=False)

        elif isinstance(raw_reply, list):
            reply = "\n".join(
                item.get("text") or item.get("pageContent") or str(item)
                for item in raw_reply if isinstance(item, (dict, str))
            )

        else:
            reply = str(raw_reply)

    except Exception as e:
        reply = f"⚠️ Error procesando respuesta de {source}: {e}"

    return enforce_language(user_question, reply, language)
