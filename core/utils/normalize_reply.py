import json
import re
import logging
from langchain_openai import ChatOpenAI


# -----------------------------------------------------
# 🔹 Limpieza robusta de respuesta cruda (sin LLM)
# -----------------------------------------------------
def normalize_reply(raw_reply, query=None, source=None):
    """
    Normaliza respuestas crudas que vienen desde MCP o agentes secundarios.
    - Soporta dicts, JSON, listas o strings planos.
    - Evita devolver valores vacíos que activen el fallback.
    - Conserva el texto incluso si el formato no es estándar.
    """
    try:
        # 🔸 Caso nulo
        if raw_reply is None:
            return ""

        # 🔸 Si viene como dict (p. ej. {"text": "..."} o {"pageContent": "..."})
        if isinstance(raw_reply, dict):
            for key in ["pageContent", "text", "content", "response"]:
                if key in raw_reply and isinstance(raw_reply[key], str):
                    val = raw_reply[key].strip()
                    if val:
                        return val
            # Devuelve el JSON como texto si no hay campos reconocibles
            return json.dumps(raw_reply, ensure_ascii=False)

        # 🔸 Si es JSON string
        if isinstance(raw_reply, str):
            try:
                obj = json.loads(raw_reply)
                if isinstance(obj, dict):
                    for key in ["pageContent", "text", "content", "response"]:
                        if key in obj and isinstance(obj[key], str):
                            val = obj[key].strip()
                            if val:
                                return val
                    # Si no hay campos reconocibles, devuelve JSON completo
                    return json.dumps(obj, ensure_ascii=False)
            except Exception:
                pass  # no era JSON válido, se trata como texto normal

        # 🔸 Si es lista (varios resultados o fragmentos)
        if isinstance(raw_reply, list):
            parts = []
            for item in raw_reply:
                if isinstance(item, dict):
                    for key in ["pageContent", "text", "content"]:
                        if key in item and item[key]:
                            parts.append(str(item[key]).strip())
                            break
                elif isinstance(item, str):
                    parts.append(item.strip())
            if parts:
                return "\n".join(parts).strip()

        # 🔸 Si es texto plano
        if isinstance(raw_reply, str):
            cleaned = re.sub(r"\s+", " ", raw_reply).strip()
            return cleaned or raw_reply

        # 🔸 Fallback final: convierte cualquier cosa a texto
        return str(raw_reply)

    except Exception as e:
        logging.error(f"⚠️ Error en normalize_reply: {e}", exc_info=True)
        return str(raw_reply) or "Respuesta no disponible"


# -----------------------------------------------------
# 💬 Post-procesamiento con LLM (estilo n8n)
# -----------------------------------------------------
def summarize_tool_output(query: str, raw_output: str, temperature: float = 0.0) -> str:
    """
    Reformula la salida cruda de una tool en una respuesta natural y amable.
    Equivale al "LLM Post-Processor" de n8n.
    """
    try:
        if not raw_output or len(raw_output.strip()) == 0:
            return "Lo siento, no encontré información relevante en este momento."

        llm = ChatOpenAI(model="gpt-4o-mini", temperature=temperature)

        prompt = f"""
            Eres el asistente virtual del hotel. Responde de forma amable,
            natural y útil al huésped, usando la información a continuación.

            Consulta del huésped:
            "{query}"

            Información encontrada:
            {raw_output}

            Instrucciones:
            - Responde en el mismo idioma que la consulta.
            - Da una respuesta breve (2–4 frases), clara y natural.
            - No uses formato JSON ni listas técnicas.
            - Si la información indica “no disponible”, responde con una frase educada explicando la situación.
            """

        response = llm.invoke(prompt)
        return response.content.strip()

    except Exception as e:
        logging.error(f"⚠️ Error en summarize_tool_output: {e}", exc_info=True)
        # fallback simple: devuelve la respuesta sin reformular
        return raw_output.strip()
