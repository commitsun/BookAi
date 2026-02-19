# core/language_manager.py
import os
import re
from functools import lru_cache
from typing import Optional, Tuple

from langchain_openai import ChatOpenAI
from langdetect import DetectorFactory, LangDetectException, detect_langs

OPENAI_MODEL = "gpt-4.1-mini"

# Fijamos seed para resultados deterministas en langdetect
DetectorFactory.seed = 0

# Mapeo simple nombre → ISO 639-1 para peticiones explícitas de idioma
LANG_ALIASES = {
    "es": {"espanol", "español", "castellano", "spanish"},
    "en": {"ingles", "inglés", "english"},
    "pt": {"portugues", "portugués", "portuguese", "português", "brasileiro"},
    "fr": {"frances", "francés", "french"},
    "it": {"italiano", "italian"},
    "de": {"aleman", "alemán", "german", "deutsch"},
    "gl": {"gallego", "galego"},
    "ca": {"catalan", "catalán"},
}

def _normalize_iso_lang_code(code: str) -> Optional[str]:
    """
    Normaliza códigos ISO de idioma.
    Acepta formato: xx o xx-yy.
    """
    value = (code or "").strip().lower().replace("_", "-")
    if re.fullmatch(r"[a-z]{2}", value):
        return value
    if re.fullmatch(r"[a-z]{2}-[a-z]{2}", value):
        return value
    return None

@lru_cache(maxsize=1)
def _ack_tokens() -> set[str]:
    """
    Lista configurable de tokens de acuse/saludo que no deben cambiar el idioma.
    Configurable vía env `LANG_ACK_TOKENS` (coma separada). Fallback: lista base.
    """
    env_tokens = os.getenv("LANG_ACK_TOKENS", "")
    tokens = {t.strip().lower() for t in env_tokens.split(",") if t.strip()}
    if tokens:
        return tokens
    return {
        "si",
        "sí",
        "yes",
        "yeah",
        "yep",
        "no",
        "nope",
        "nop",
        "ok",
        "okay",
        "okey",
        "oki",
        "ciao",
        "chao",
        "vale",
        "perfect",
        "perfecto",
        "done",
        "gracias",
        "thanks",
        "thankyou",
        "thankyou!",
        "merci",
        "danke",
        "obrigado",
        "obrigada",
    }


def _normalize_ack(text: str) -> str:
    """
    Normaliza un acuse breve para compararlo con la lista de tokens.
    - Lowercase
    - Quita espacios y signos de puntuación simples
    - Reduce repeticiones largas (okkk -> okk)
    """
    if not text:
        return ""
    txt = text.lower()
    txt = txt.strip("¡!.,;:-¿?\"'[](){} ")
    txt = re.sub(r"\s+", "", txt)
    txt = re.sub(r"(.)\1{2,}", r"\1\1", txt)
    return txt


def _short_greeting_lang(text: str) -> Optional[str]:
    """
    Heurística rápida para saludos cortos que langdetect suele confundir.
    """
    token = (text or "").strip().lower()
    greetings = {
        "hola": "es",
        "buenas": "es",
        "hello": "en",
        "hi": "en",
        "hey": "en",
        "bonjour": "fr",
        "salut": "fr",
        "ciao": "it",
        "ola": "pt",  # sin tilde, típico portugués
        "olá": "pt",
        "oi": "pt",
    }
    return greetings.get(token)


def _langdetect_guess(text: str) -> Optional[Tuple[str, float]]:
    """
    Intenta detectar idioma de forma rápida sin LLM.
    Devuelve (lang, prob) o None si no se puede determinar.
    """
    if not text:
        return None
    try:
        candidates = detect_langs(text)
        if not candidates:
            return None
        best = candidates[0]
        code = (best.lang or "").strip().lower()
        if len(code) != 2:
            return None
        return code, best.prob
    except LangDetectException:
        return None
    except Exception:
        return None


def _explicit_language_request(text: str) -> Optional[str]:
    """
    Detecta si el usuario pide explícitamente hablar en un idioma concreto.
    Busca combinaciones de verbos típicos + nombre de idioma para evitar falsos positivos.
    """
    if not text:
        return None

    txt = (text or "").lower()
    keywords = [
        "habla",
        "hablar",
        "responde",
        "respóndeme",
        "contesta",
        "puedes",
        "podemos",
        "hablemos",
        "idioma",
        "cambia",
        "cambiar",
        "language",
        "speak",
        "reply",
        "respond",
        "write",
        "talk",
    ]

    if not any(word in txt for word in keywords):
        return None

    for code, aliases in LANG_ALIASES.items():
        for alias in aliases:
            if re.search(rf"\b{alias}\b", txt):
                return code

    return None


def _is_short_ambiguous_snippet(text: str) -> bool:
    """
    Detecta fragmentos muy cortos y ambiguos (p.ej. nombres de hotel/ciudad)
    que no deberían forzar un cambio de idioma por sí solos.
    """
    raw = (text or "").strip()
    if not raw:
        return False
    if any(ch in raw for ch in ".!?;:"):
        return False
    words = [w for w in raw.split() if w]
    if len(words) > 2:
        return False
    if len(raw) > 28:
        return False
    if re.search(r"\d", raw):
        return False
    return True


def _is_low_information_followup(text: str) -> bool:
    """
    Detecta mensajes cortos de seguimiento (fechas, cantidades, datos sueltos)
    donde NO conviene cambiar de idioma si ya hay uno previo estable.
    """
    raw = (text or "").strip()
    if not raw:
        return False
    words = [w for w in re.split(r"\s+", raw) if w]
    if len(words) > 4:
        return False
    if len(raw) > 32:
        return False
    if any(ch in raw for ch in ".!?;:"):
        return False
    # Muy típico en reservas: "2 adults", "24 to 26", "2 personas", etc.
    has_digit = bool(re.search(r"\d", raw))
    return has_digit or len(words) <= 2


class LanguageManager:
    """
    Gestión de idioma + tono diplomático hacia el huésped.
    """

    def __init__(self, model: Optional[str] = None, temperature: float = 0.0):
        self.llm = ChatOpenAI(model=model or OPENAI_MODEL, temperature=temperature)

    def _llm_detect_lang_code(self, text: str, fallback: str = "es") -> str:
        prompt = [
            {
                "role": "system",
                "content": (
                    "You are a language detector. "
                    "Return ONLY the ISO 639-1 language code (lowercase) of the user's message. "
                    "If unsure, return the provided fallback code. No explanations."
                ),
            },
            {"role": "user", "content": f"Fallback: {fallback}\nText:\n{text}"},
        ]
        try:
            out = self.llm.invoke(prompt).content.strip().lower()
            out = out.split()[0].strip(" .,:;|[](){}\"'") if out else fallback
            return _normalize_iso_lang_code(out) or (fallback or "es")
        except Exception:
            return fallback or "es"

    @lru_cache(maxsize=4096)
    def detect_language(self, text: str, prev_lang: Optional[str] = None) -> str:
        raw_text = (text or "").strip()
        # Si vienen varios mensajes combinados (con saltos de línea), usa la última línea real.
        # Pero si esa última línea es demasiado "telegráfica" (ej. "y parking?"),
        # toma una línea previa con más señal para evitar arrastrar idioma anterior.
        if "\n" in raw_text:
            parts = [p.strip() for p in raw_text.split("\n") if p.strip()]
            if parts:
                candidate = parts[-1]
                if _is_low_information_followup(candidate) or _is_short_ambiguous_snippet(candidate):
                    for prior in reversed(parts[:-1]):
                        if not _is_low_information_followup(prior) and not _is_short_ambiguous_snippet(prior):
                            candidate = prior
                            break
                text = candidate
            else:
                text = raw_text
        else:
            text = raw_text

        base_lang = _normalize_iso_lang_code(prev_lang or "") or "es"

        if not text:
            return base_lang

        explicit = _explicit_language_request(text)
        if explicit:
            return _normalize_iso_lang_code(explicit) or base_lang

        # Evita cambiar de idioma por acuses/saludos cortos
        normalized = _normalize_ack(text)
        if normalized in _ack_tokens():
            return base_lang

        # Si ya hay idioma previo, no lo cambies por mensajes "de dato corto"
        # (cantidades, fechas, respuestas telegráficas típicas de reservas).
        if prev_lang and _is_low_information_followup(text):
            return base_lang

        # Mensajes de una sola palabra y cortos (saludos/acuses)
        words = text.split()
        if len(words) == 1 and len(text) <= 10:
            direct = _short_greeting_lang(text)
            if direct:
                return direct
            guess = _langdetect_guess(text)
            if guess:
                code, prob = guess
                threshold = 0.8
                if prev_lang and code != base_lang and prob < 0.92:
                    return base_lang
                if prob >= threshold:
                    normalized = _normalize_iso_lang_code(code)
                    return normalized or base_lang
            # Con una sola palabra, si ya hay idioma previo, no forzar cambios por LLM.
            if prev_lang:
                return base_lang
            # como último recurso, intenta con LLM breve
            normalized = self._llm_detect_lang_code(text, fallback=base_lang)
            if normalized:
                return normalized
            return base_lang

        # Paso 1: heurística rápida con langdetect
        guess = _langdetect_guess(text)
        if guess:
            code, prob = guess
            threshold_env = os.getenv("LANGDETECT_THRESHOLD")
            try:
                threshold = float(threshold_env) if threshold_env else 0.75
            except ValueError:
                threshold = 0.75

            if prob >= threshold:
                if prev_lang and code != base_lang and (
                    _is_short_ambiguous_snippet(text) or _is_low_information_followup(text)
                ):
                    return base_lang
                normalized = _normalize_iso_lang_code(code)
                # Verificación con LLM cuando el detector difiere del idioma previo
                # o cuando la confianza no es alta, para evitar falsos positivos.
                if normalized and (normalized != base_lang or prob < 0.90):
                    llm_code = self._llm_detect_lang_code(text, fallback=normalized)
                    if llm_code and llm_code != normalized:
                        normalized = llm_code
                if prev_lang and normalized and normalized != base_lang and _is_low_information_followup(text):
                    return base_lang
                return normalized or base_lang
            if prev_lang:
                return base_lang

        try:
            normalized = self._llm_detect_lang_code(text, fallback=base_lang)
            if not normalized:
                return base_lang
            if prev_lang and normalized != base_lang and (
                _is_short_ambiguous_snippet(text) or _is_low_information_followup(text)
            ):
                return base_lang
            return normalized
        except Exception as e:
            print(f"⚠️ Error detectando idioma: {e}")
            return base_lang

    def ensure_language(self, text: str, lang_code: str) -> str:
        if not text:
            return text

        lang_code = _normalize_iso_lang_code(lang_code or "") or "es"
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
