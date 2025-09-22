from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI

# =========
# LLM para idioma y estilo
# =========
llm_language = ChatOpenAI(model="gpt-4o-mini", temperature=0.3)
llm_detect = ChatOpenAI(model="gpt-4o-mini", temperature=0)  # 👈 detector estable

# =========
# Modelos de datos
# =========
class LangDetect(BaseModel):
    language: str = Field(..., description="Idioma detectado en código ISO-639-1 (ej. es, en, fr, ar)")

# =========
# Detección de idioma
# =========
def detect_language(text: str) -> str:
    """
    Detecta el idioma del texto y devuelve su código ISO-639-1.
    Ejemplo: 'es', 'en', 'fr', 'ar'.
    """
    try:
        structured = llm_detect.with_structured_output(LangDetect)
        result = structured.invoke([
            {
                "role": "system",
                "content": (
                    "Detecta el idioma del siguiente texto y responde "
                    "únicamente con el código ISO-639-1 (ej. 'es', 'en', 'fr', 'ar')."
                )
            },
            {"role": "user", "content": text},
        ])
        return result.language
    except Exception:
        return "es"  # 👈 fallback seguro

# =========
# Enforcer de idioma y estilo
# =========
def enforce_language(user_msg: str, reply: str, lang: str | None = None) -> str:
    target_lang = lang if lang else "el idioma del usuario"

    system_prompt = (
        f"Responde SIEMPRE en {target_lang}. "
        "No traduzcas literalmente, adapta la respuesta para sonar natural. "
        "Usa un tono humano, cálido y cercano. "
        "No repitas saludos innecesarios. "
        "Puedes usar emojis ligeros si encajan 🙂."
    )

    enforced = llm_language.invoke([
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": f"Respuesta propuesta: {reply}"},
        {"role": "user", "content": user_msg},
    ])

    return enforced.content.encode("utf-8", errors="replace").decode("utf-8")
