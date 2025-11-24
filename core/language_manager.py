# core/language_manager.py
import os
from functools import lru_cache
from typing import Optional
from langchain_openai import ChatOpenAI

OPENAI_MODEL = "gpt-4.1-mini"


class LanguageManager:
    """
    Gestión de idioma + tono diplomático hacia el huésped.
    """

    def __init__(self, model: Optional[str] = None, temperature: float = 0.0):
        self.llm = ChatOpenAI(model=model or OPENAI_MODEL, temperature=temperature)

    @lru_cache(maxsize=4096)
    def detect_language(self, text: str, prev_lang: Optional[str] = None) -> str:
        text = (text or "").strip()
        if not text:
            return (prev_lang or "es")

        # Evita cambiar de idioma por acuses/saludos cortos
        normalized = text.lower().strip("¡!.,;:-¿?\"'[](){} ")
        ack_tokens = {
            "ok",
            "okay",
            "okey",
            "oki",
            "ciao",
            "chao",
        }
        if normalized in ack_tokens:
            return (prev_lang or "es")

        prompt = [
            {
                "role": "system",
                "content": (
                    "You are a language detector. "
                    "Return ONLY the ISO 639-1 language code (lowercase) of the user's message. "
                    "If unsure, return 'es'. No explanations."
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
        if not text:
            return text

        lang_code = (lang_code or "es").lower().strip()
        prompt = [
            {
                "role": "system",
                "content": (
                    "You are a precise rewriter. "
                    "Output the SAME content as the user's message but strictly in the target language. "
                    "Do not add explanations, prefaces, or any extra text. "
                    "No code fences. No emoji changes. Keep meaning and tone."
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

    def translate_if_needed(self, text: str, lang_from: str, lang_to: str) -> str:
        lf = (lang_from or "").strip().lower()
        lt = (lang_to or "").strip().lower()
        if lf and lt and lf == lt:
            return text
        return self.ensure_language(text, lt or "es")

    def short_phrase(self, meaning: str, lang_code: str) -> str:
        lang_code = (lang_code or "es").lower().strip()
        prompt = [
            {
                "role": "system",
                "content": (
                    "You generate one short, natural sentence in the requested language. "
                    "No explanations. No extra text."
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

    def polish_for_guest(self, raw_message: str, guest_lang: str) -> str:
        """
        Pulido diplomático:
        - Mantiene el mismo mensaje base.
        - Tono profesional, calmado y respetuoso estilo atención hotelera.
        - Firme si hace falta poner límites.
        - Nada de regañinas agresivas tipo 'no son formas'.
        - Sin disculparse en exceso ni humillarse.
        - Devuelve SOLO el mensaje final en el idioma guest_lang.
        """
        guest_lang = (guest_lang or "es").strip().lower()

        prompt = [
            {
                "role": "system",
                "content": (
                    "You are the Guest Relations Manager of a hotel. "
                    "Rewrite the manager's message so it sounds polite, respectful, "
                    "and professional, like high-quality hotel customer service. "
                    "You may soften harsh phrasing, but you MUST keep the manager's intent "
                    "(boundaries, policies, warnings, clarifications). "
                    "Be concise. Do NOT add apologies unless the manager clearly apologized. "
                    "Do NOT add threats. "
                    "Return ONLY the final sentence(s), with no explanations."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Target language (ISO 639-1): {guest_lang}\n"
                    f"Original manager message:\n{raw_message}"
                ),
            },
        ]

        try:
            return self.llm.invoke(prompt).content.strip()
        except Exception as e:
            print(f"⚠️ Error puliendo respuesta del encargado: {e}")
            # fallback: si algo peta, mandamos el mensaje tal cual
            return raw_message


# singleton global
language_manager = LanguageManager()
