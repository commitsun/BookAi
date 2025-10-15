# core/language_manager.py
import os
from functools import lru_cache
from typing import Optional
from langchain_openai import ChatOpenAI

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

class LanguageManager:
    """
    Gestión de idioma sin hardcode:
    - Detecta idioma (ISO 639-1) con OpenAI
    - Reescribe/Traduce forzando idioma destino
    - Idempotente: si ya está en ese idioma, no “añade” nada
    """

    def __init__(self, model: Optional[str] = None, temperature: float = 0.0):
        self.llm = ChatOpenAI(model=model or OPENAI_MODEL, temperature=temperature)

    @lru_cache(maxsize=4096)
    def detect_language(self, text: str) -> str:
        """
        Devuelve el código ISO 639-1 (ej: 'es', 'en', 'fr', 'de', 'it', 'pt', ...)
        No usa listas ni palabras clave. Nada hardcodeado.
        """
        text = (text or "").strip()
        if not text:
            return "es"

        prompt = [
            {
                "role": "system",
                "content": (
                    "You are a language detector. "
                    "Return ONLY the ISO 639-1 language code (lowercase) of the user's message. "
                    "If unsure, return 'es'. No extra text."
                ),
            },
            {"role": "user", "content": text}
        ]
        out = self.llm.invoke(prompt).content.strip().lower()
        # normaliza posibles respuestas largas
        out = out.split()[0].strip(" .,:;|[](){}\"'") if out else "es"
        if len(out) != 2:  # si no es iso-639-1, fallback seguro
            return "es"
        return out

    def ensure_language(self, text: str, lang_code: str) -> str:
        """
        Reescribe 'text' en el idioma 'lang_code' SIN añadir información ni formato extra.
        """
        if not text:
            return text

        lang_code = (lang_code or "es").lower().strip()
        prompt = [
            {
                "role": "system",
                "content": (
                    "You are a precise rewriter. "
                    "Output the SAME content as the user's message but strictly in the target language. "
                    "Do not add explanations, prefaces, or any extra text. No code fences. No emojis changes."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Target language (ISO 639-1): {lang_code}\n"
                    f"---\n{text}"
                ),
            },
        ]
        out = self.llm.invoke(prompt).content.strip()
        return out

    def short_phrase(self, meaning: str, lang_code: str) -> str:
        """
        Genera una frase MUY breve con el significado solicitado en el idioma destino.
        Útil para mensajes estandarizados (ej.: ‘un momento por favor…’).
        """
        lang_code = (lang_code or "es").lower().strip()
        prompt = [
            {
                "role": "system",
                "content": (
                    "You generate very short, natural sentences in the requested language. "
                    "No explanations. No extra text. Keep the meaning and tone."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Language: {lang_code}\n"
                    f"Meaning (Spanish): {meaning}\n"
                    "Return only one short sentence."
                ),
            },
        ]
        return self.llm.invoke(prompt).content.strip()

# Singleton cómodo
language_manager = LanguageManager()
