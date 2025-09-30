import json
from core.language import enforce_language

def normalize_reply(raw_reply, user_question, language=None, source="InfoAgent") -> str:
    """
    Convierte la respuesta cruda en texto limpio y forzado al idioma correcto.
    """
    final_reply = ""

    try:
        if isinstance(raw_reply, str):
            try:
                data = json.loads(raw_reply)
                if isinstance(data, dict) and "pageContent" in data:
                    final_reply = data["pageContent"]
                else:
                    final_reply = raw_reply
            except Exception:
                final_reply = raw_reply

        elif isinstance(raw_reply, dict):
            if "respuesta" in raw_reply:
                final_reply = raw_reply["respuesta"]
            elif "pageContent" in raw_reply:
                final_reply = raw_reply["pageContent"]
            else:
                final_reply = json.dumps(raw_reply, ensure_ascii=False)

        elif isinstance(raw_reply, list):
            texts = []
            for item in raw_reply:
                if isinstance(item, dict):
                    if "text" in item:
                        texts.append(item["text"])
                    elif "pageContent" in item:
                        texts.append(item["pageContent"])
                elif isinstance(item, str):
                    texts.append(item)
            final_reply = "\n".join(texts)

        else:
            final_reply = str(raw_reply)

    except Exception as e:
        final_reply = f"⚠️ Error procesando respuesta de {source}: {e}"

    final_reply = enforce_language(user_question, final_reply, language)
    preview = final_reply[:200].replace("\n", " ")
    #print(f"🟢 CLEAN REPLY ({source}): {preview}...")
    return final_reply
