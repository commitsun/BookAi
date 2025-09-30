import os
from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI

api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise RuntimeError("❌ Falta la variable OPENAI_API_KEY en el entorno.")

# LLMs
llm_language = ChatOpenAI(model="gpt-4o-mini", temperature=0.3)
llm_detect = ChatOpenAI(model="gpt-4o-mini", temperature=0)

class LangDetect(BaseModel):
    language: str = Field(..., description="Código ISO-639-1 detectado (ej. es, en, fr)")

def detect_language(text: str) -> str:
    """Devuelve el idioma detectado o 'es' como fallback."""
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
    """Fuerza idioma y estilo humano en la respuesta."""
    target_lang = lang or "idioma del usuario"
    system_prompt = (
        f"Responde SIEMPRE en {target_lang}. "
        "Adapta la respuesta para sonar natural y profesional, como un humano. "
        "Evita frases de cierre genéricas o emojis innecesarios. "
        "Si no tienes la información, responde: 'No dispongo de ese dato en este momento.'"
    )
    enforced = llm_language.invoke([
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": f"Respuesta propuesta: {reply}"},
        {"role": "user", "content": user_msg},
    ])
    return enforced.content.encode("utf-8", errors="replace").decode("utf-8")
