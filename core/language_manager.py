# core/language_manager.py
import os
from functools import lru_cache
from typing import Optional
from langchain_openai import ChatOpenAI

# 🧠 Hardcodeamos el modelo para evitar error 400 en detección de idioma
OPENAI_MODEL = "gpt-4.1-mini"


class LanguageManager:
    """
    Gestión de idioma:
    - Detecta idioma (ISO 639-1) con OpenAI
    - Reescribe/Traduce forzando idioma destino
    - Idempotente: si ya está en ese idioma, no “añade” nada
    """

    def __init__(self, model: Optional[str] = None, temperature: float = 0.0):
        # Se asegura de tener un modelo válido siempre
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
            {"role": "user", "content": text},
        ]

        try:
            out = self.llm.invoke(prompt).content.strip().lower()
            out = out.split()[0].strip(" .,:;|[](){}\"'") if out else "es"
            if len(out) != 2:
                return "es"
            return out
        except Exception as e:
            print(f"⚠️ Error detectando idioma: {e}")
            return "es"

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
                    "Do not add explanations, prefaces, or any extra text. No code fences. No emoji changes."
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

        try:
            return self.llm.invoke(prompt).content.strip()
        except Exception as e:
            print(f"⚠️ Error forzando idioma: {e}")
            return text

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

        try:
            return self.llm.invoke(prompt).content.strip()
        except Exception as e:
            print(f"⚠️ Error generando frase corta: {e}")
            return meaning


# Singleton global para uso compartido
language_manager = LanguageManager()
