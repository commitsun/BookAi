import json
import logging
from core.language import enforce_language

logger = logging.getLogger(__name__)

def normalize_reply(raw_reply, user_question, language=None, source="Agent"):
    """
    Normaliza la respuesta cruda de un agente MCP/n8n y la adapta al idioma.
    """
    final_reply = None

    try:
        if isinstance(raw_reply, str):
            try:
                data = json.loads(raw_reply)
                if isinstance(data, dict):
                    final_reply = data.get("pageContent") or data.get("respuesta") or raw_reply
                else:
                    final_reply = raw_reply
            except json.JSONDecodeError:
                final_reply = raw_reply

        elif isinstance(raw_reply, dict):
            final_reply = raw_reply.get("respuesta") or raw_reply.get("pageContent") or json.dumps(raw_reply, ensure_ascii=False)

        elif isinstance(raw_reply, list):
            texts = []
            for item in raw_reply:
                if isinstance(item, dict):
                    texts.append(item.get("text") or item.get("pageContent", ""))
                elif isinstance(item, str):
                    texts.append(item)
            final_reply = "\n".join([t for t in texts if t.strip()])

        else:
            final_reply = str(raw_reply)

    except Exception as e:
        logger.error(f"Error procesando respuesta de {source}: {e}")
        final_reply = None

    if not final_reply or not final_reply.strip():
        final_reply = "No dispongo de ese dato en este momento."

    final_reply = enforce_language(user_question, final_reply, language)

    preview = final_reply[:150].replace("\n", " ")
    logger.info(f"🟢 CLEAN REPLY ({source}): {preview}...")

    return final_reply
