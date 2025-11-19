import json
import logging
from typing import Any

log = logging.getLogger("normalize_reply")

def _extract_text_from_raw(raw: Any) -> str:
    """
    Limpia respuestas del MCP y añade soporte para:
    - Estructuras tipo retriever (list + dict + pageContent)
    - Diccionarios simples con 'text'
    - Strings JSON
    - Estructuras Gemini:
        [
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [ {"text": "..."} ]
                        }
                    }
                ]
            }
        ]
    """
    if raw is None:
        return ""

    # --------------------------------------------------------
    # 1) Si es LISTA → analizar cada elemento (sin romper tu lógica)
    # --------------------------------------------------------
    if isinstance(raw, list):
        texts = []
        for item in raw:
            # PRIMERO: intentamos extracción profunda (Gemini, dicts, etc.)
            extracted = _extract_text_from_raw(item)
            if extracted:
                texts.append(extracted)
                continue

            # SEGUNDO: tu lógica original (dict con text)
            try:
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

    # --------------------------------------------------------
    # 2) Si es DICCIONARIO
    # --------------------------------------------------------
    if isinstance(raw, dict):

        # 🔥 Soporte para formato GEMINI (candidates → content → parts → text)
        if "candidates" in raw:
            try:
                candidates = raw["candidates"]
                if isinstance(candidates, list) and candidates:
                    content = candidates[0].get("content", {})
                    parts = content.get("parts", [])
                    if isinstance(parts, list) and parts:
                        txt = parts[0].get("text")
                        if txt:
                            return txt
            except Exception as e:
                log.warning(f"⚠️ Error procesando estructura Gemini: {e}")

        # ✔️ Tu lógica original
        if "pageContent" in raw:
            return raw["pageContent"]
        if "text" in raw:
            return raw["text"]

        # fallback dict → string
        return json.dumps(raw, ensure_ascii=False)

    # --------------------------------------------------------
    # 3) Si es STRING (puede ser JSON)
    # --------------------------------------------------------
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return _extract_text_from_raw(parsed)
        except json.JSONDecodeError:
            return raw

    # --------------------------------------------------------
    # 4) fallback genérico
    # --------------------------------------------------------
    return str(raw)


def normalize_reply(raw_reply: Any, user_query: str, agent_name: str = "Unknown") -> str:
    """Limpia caracteres y artefactos del modelo, sin censurar contenido."""
    try:
        text = _extract_text_from_raw(raw_reply)
        if not text or len(text.strip()) == 0:
            return f"{agent_name} no pudo generar una respuesta adecuada."

        # Conservamos tu limpieza original
        text = (
            text.replace("\\n", "\n")
                .replace("**", "")
                .replace("```", "")
                .strip()
        )
        return text

    except Exception as e:
        log.error(f"Error en normalize_reply: {e}", exc_info=True)
        return f"Error procesando respuesta del agente {agent_name}."
