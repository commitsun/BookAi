import json
import re
import logging
from langchain_openai import ChatOpenAI


# -----------------------------------------------------
# 🔹 Limpieza robusta de respuesta cruda (sin LLM)
# -----------------------------------------------------
import json, re, logging

def normalize_reply(raw_reply, query=None, source=None):
    """
    Normaliza respuestas crudas desde MCP o agentes secundarios.
    - Desanida múltiples niveles de JSON.
    - Extrae 'pageContent', 'text' o 'content' de cualquier estructura.
    - Nunca devuelve vacío: conserva texto si algo es legible.
    """
    try:
        if raw_reply is None:
            return ""

        # 🔁 Desanidar JSON en profundidad (hasta 3 niveles)
        def deep_deserialize(obj):
            for _ in range(3):
                if isinstance(obj, str):
                    try:
                        obj = json.loads(obj)
                    except Exception:
                        break
            return obj

        obj = deep_deserialize(raw_reply)

        # 🔹 Si es lista → concatenar contenidos útiles
        if isinstance(obj, list):
            fragments = []
            for item in obj:
                item = deep_deserialize(item)
                if isinstance(item, dict):
                    val = item.get("pageContent") or item.get("text") or item.get("content")
                    if isinstance(val, str):
                        val = deep_deserialize(val)
                        if isinstance(val, dict):
                            val = val.get("pageContent") or val.get("text") or val.get("content")
                        if val:
                            fragments.append(val.strip())
                elif isinstance(item, str):
                    fragments.append(item.strip())
            return "\n".join(fragments).strip()

        # 🔹 Si es dict → devolver texto principal
        if isinstance(obj, dict):
            for key in ("pageContent", "text", "content"):
                if key in obj and isinstance(obj[key], str):
                    return obj[key].strip()
            return json.dumps(obj, ensure_ascii=False)

        # 🔹 Si es texto plano
        if isinstance(obj, str):
            return re.sub(r"\s+", " ", obj).strip()

        # 🔹 Fallback final
        return str(obj).strip()

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

        llm = ChatOpenAI(model="gpt-4.1-mini", temperature=temperature)

        # Detectar idioma simple
        lang_hint = "español" if re.search(r"[áéíóúñ¿¡]", query or "", re.I) or query.lower().startswith(("hola", "buen", "gracias")) else "auto"

        prompt = f"""
        Eres el asistente virtual del hotel. Responde de forma amable,
        natural y útil al huésped, usando la información a continuación.

        Consulta del huésped:
        "{query}"

        Información encontrada:
        {raw_output}

        Instrucciones:
        - Responde en el mismo idioma que la consulta (o en {lang_hint} si no se detecta idioma claro).
        - Da una respuesta breve (2–4 frases), clara y natural.
        - No uses formato JSON ni listas técnicas.
        """



        response = llm.invoke(prompt)
        return response.content.strip()

    except Exception as e:
        logging.error(f"⚠️ Error en summarize_tool_output: {e}", exc_info=True)
        # fallback simple: devuelve la respuesta sin reformular
        return raw_output.strip()
