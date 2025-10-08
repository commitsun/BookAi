# =====================================================
# 🧠 normalize_reply.py — Limpieza y post-procesamiento de respuestas
# =====================================================
import json
import re
import logging
from langchain_openai import ChatOpenAI

# -----------------------------------------------------
# 🔹 Limpieza básica de respuesta cruda (sin LLM)
# -----------------------------------------------------
def normalize_reply(raw_reply, query=None, source=None):
    """
    Normaliza respuestas crudas que vienen desde MCP o agentes secundarios.
    - Elimina envoltorios JSON
    - Limpia Markdown, metadatos y duplicados
    """
    try:
        if not raw_reply:
            return ""

        # Si viene como JSON con "pageContent"
        if isinstance(raw_reply, str):
            try:
                obj = json.loads(raw_reply)
                if isinstance(obj, dict) and "pageContent" in obj:
                    return obj["pageContent"]
            except Exception:
                pass

        # Si es lista de resultados con "pageContent"
        if isinstance(raw_reply, list):
            parts = []
            for item in raw_reply:
                if isinstance(item, dict) and "text" in item:
                    try:
                        data = json.loads(item["text"])
                        parts.append(data.get("pageContent", item["text"]))
                    except Exception:
                        parts.append(item["text"])
                elif isinstance(item, dict) and "pageContent" in item:
                    parts.append(item["pageContent"])
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(parts)

        # Si ya es texto limpio
        if isinstance(raw_reply, str):
            cleaned = re.sub(r"\s+", " ", raw_reply)
            return cleaned.strip()

        return str(raw_reply)

    except Exception as e:
        logging.error(f"⚠️ Error en normalize_reply: {e}")
        return str(raw_reply)


# -----------------------------------------------------
# 💬 Post-procesamiento con LLM (estilo n8n)
# -----------------------------------------------------
def summarize_tool_output(query: str, raw_output: str, temperature: float = 0.0) -> str:
    """
    Reformula la salida cruda de una tool en una respuesta natural y amigable.
    Equivale al "LLM Post-Processor" de n8n.
    """
    try:
        if not raw_output or len(raw_output.strip()) == 0:
            return "Lo siento, no encontré información relevante en este momento."

        llm = ChatOpenAI(model="gpt-4o-mini", temperature=temperature)

        prompt = f"""
Eres el asistente virtual del hotel. Debes responder de manera amable,
natural y útil al huésped, usando la información a continuación.

Consulta del huésped:
"{query}"

Información encontrada:
{raw_output}

Instrucciones:
- Responde en el mismo idioma que la consulta.
- Redacta una sola respuesta breve (2–4 frases).
- No muestres formato JSON ni listas técnicas.
- Si la información incluye “no disponible”, responde educadamente explicando la situación.
"""

        response = llm.invoke(prompt)
        return response.content.strip()

    except Exception as e:
        logging.error(f"⚠️ Error en summarize_tool_output: {e}", exc_info=True)
        # fallback simple
        return raw_output.strip()
