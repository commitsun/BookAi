import json
import logging
from typing import Any

log = logging.getLogger("normalize_reply")

def _extract_text_from_raw(raw: Any) -> str:
    """
    Limpia respuestas del MCP que vienen como listas de objetos JSON
    con campos `type`, `text`, `pageContent`, etc.
    """
    if raw is None:
        return ""

    # Caso: lista de items (respuesta tipo retriever)
    if isinstance(raw, list):
        texts = []
        for item in raw:
            try:
                # Ejemplo: {"type": "text", "text": "{\"pageContent\": \"...\"}"}
                if isinstance(item, dict) and "text" in item:
                    txt = item["text"]
                    try:
                        inner = json.loads(txt)
                        if isinstance(inner, dict) and "pageContent" in inner:
                            texts.append(inner["pageContent"])
                        else:
                            texts.append(txt)
                    except json.JSONDecodeError:
                        texts.append(txt)
                elif isinstance(item, str):
                    texts.append(item)
            except Exception as e:
                log.warning(f"⚠️ Error procesando fragmento: {e}")
        return "\n".join(texts)

    # Caso: diccionario
    if isinstance(raw, dict):
        if "pageContent" in raw:
            return raw["pageContent"]
        if "text" in raw:
            return raw["text"]
        return json.dumps(raw, ensure_ascii=False)

    # Caso: string JSON
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and "pageContent" in parsed:
                return parsed["pageContent"]
            elif isinstance(parsed, list):
                return _extract_text_from_raw(parsed)
        except json.JSONDecodeError:
            pass
        return raw

    # Caso fallback
    return str(raw)


def normalize_reply(raw_reply: Any, user_query: str, agent_name: str = "Unknown") -> str:
    """Limpia caracteres y artefactos del modelo, sin censurar contenido."""
    try:
        text = _extract_text_from_raw(raw_reply)
        if not text or len(text.strip()) == 0:
            return f"{agent_name} no pudo generar una respuesta adecuada."

        text = text.replace("\\n", "\n").replace("**", "").replace("```", "").strip()
        return text
    except Exception as e:
        log.error(f"Error en normalize_reply: {e}", exc_info=True)
        return f"Error procesando respuesta del agente {agent_name}."

