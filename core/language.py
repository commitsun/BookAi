import os
import logging
from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI

api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise RuntimeError("❌ Falta la variable OPENAI_API_KEY en el entorno.")

# Inicialización de modelos
llm_language = ChatOpenAI(model="gpt-4o-mini", temperature=0.3)
llm_detect = ChatOpenAI(model="gpt-4o-mini", temperature=0)

# Modelo estructurado para detección de idioma
class LangDetect(BaseModel):
    language: str = Field(..., description="Código ISO-639-1 detectado (ej. es, en, fr)")

def detect_language(text: str) -> str:
    """Detecta el idioma del texto y devuelve un código ISO (fallback a 'es')."""
    try:
        structured = llm_detect.with_structured_output(LangDetect)
        result = structured.invoke([
            {"role": "system", "content": "Detecta el idioma y responde con código ISO-639-1."},
            {"role": "user", "content": text},
        ])
        return result.language
    except Exception:
        return "es"

def enforce_language(user_msg: str, reply: str, lang: str | None = None) -> str:
    """
    Asegura que la respuesta esté en el mismo idioma y tono que el usuario.
    ✳️ Si la respuesta ya parece final (contiene símbolos o formato de tool), 
    se devuelve tal cual sin reescribirla.
    """
    if not reply or not reply.strip():
        return "No dispongo de ese dato en este momento."

    # 🚫 No reescribimos si la respuesta ya es final
    lower_reply = reply.lower()
    final_markers = ["✅", "disponibilidad", "reserva confirmada", "🏨", "habitaciones disponibles"]
    if any(marker in lower_reply for marker in final_markers):
        logging.info(f"🟢 enforce_language: respuesta final detectada, se envía sin reescritura → {reply[:80]}")
        return reply

    # ✅ Aplicamos reescritura solo si no es final
    target_lang = lang or "idioma del usuario"
    system_prompt = (
        f"Responde SIEMPRE en {target_lang}. "
        "Adapta la respuesta para sonar natural, profesional y humana. "
        "Evita frases de cierre genéricas o emojis innecesarios. "
        "Si no tienes la información, responde: 'No dispongo de ese dato en este momento.'"
    )

    try:
        logging.info("🔄 enforce_language: reescribiendo respuesta intermedia para mantener tono natural.")
        enforced = llm_language.invoke([
            {"role": "system", "content": system_prompt},
            {"role": "system", "content": f"Respuesta propuesta: {reply}"},
            {"role": "user", "content": user_msg},
        ])
        return enforced.content.encode("utf-8", errors="replace").decode("utf-8")
    except Exception as e:
        logging.warning(f"⚠️ enforce_language: error durante reescritura → {e}")
        return reply
