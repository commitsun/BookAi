"""
Quick response rules — deterministic shortcuts that bypass the supervisor
and LLM entirely. Zero token cost, instant response.

Rules are evaluated in order. First match wins.

1. Human intervention: guest explicitly asks for a human → escalate
2. Anti-duplicate: guest confirms after a substantial AI response → short ack
"""

import random
import re

# ── Human intervention patterns ──────────────────────────────────────

_HUMAN_PATTERNS = [
    # Spanish
    r"(?:quiero|necesito|puedo)\s+hablar\s+con\s+(?:una?\s+)?(?:persona|alguien|humano|recepci[oó]n|encargad[oa]|responsable|director)",
    r"(?:p[aá]same|comun[ií]came|conect[aá]me)\s+con\s+(?:una?\s+)?(?:persona|alguien|recepci[oó]n|encargad[oa])",
    r"(?:hablar|contactar)\s+con\s+(?:una?\s+)?persona\s+real",
    r"no\s+(?:quiero|necesito)\s+(?:un\s+)?(?:bot|chatbot|robot|ia|inteligencia artificial)",
    # English
    r"(?:i\s+want|i\s+need|can\s+i|let\s+me)\s+(?:to\s+)?(?:talk|speak|chat)\s+(?:to|with)\s+(?:a\s+)?(?:person|someone|human|reception|manager|staff)",
    r"(?:transfer|connect)\s+me\s+to\s+(?:a\s+)?(?:person|someone|human|reception|agent)",
    r"(?:real|actual)\s+(?:person|human)",
    r"(?:no|stop)\s+(?:bot|chatbot|ai)",
    # French
    r"(?:je\s+veux|puis-je)\s+parler\s+[àa]\s+(?:une?\s+)?(?:personne|quelqu'un|r[ée]ception)",
    # Portuguese
    r"(?:quero|preciso)\s+falar\s+com\s+(?:uma?\s+)?(?:pessoa|algu[ée]m|recep[çc][ãa]o)",
]

_HUMAN_RE = re.compile("|".join(_HUMAN_PATTERNS), re.IGNORECASE)


def detect_human_request(message: str) -> bool:
    """Returns True if the message explicitly asks for a human. Deterministic."""
    return bool(_HUMAN_RE.search(message))


# ── Anti-duplicate: short confirmations after substantial AI response ─

_CONFIRMATION_WORDS = {
    # Spanish
    "ok", "vale", "gracias", "perfecto", "genial", "de acuerdo", "entendido",
    "bien", "estupendo", "muchas gracias", "fenomenal", "excelente",
    # English
    "thanks", "thank you", "great", "perfect", "got it", "understood",
    "okay", "cool", "awesome", "wonderful", "excellent",
    # French
    "merci", "parfait", "d'accord", "super", "génial", "compris",
    # Portuguese
    "obrigado", "obrigada", "perfeito", "combinado", "entendido",
}

_ACK_RESPONSES = {
    "es": [
        "¡De nada! Si necesitas algo más, aquí estoy.",
        "¡Perfecto! No dudes en preguntar si surge algo.",
        "¡Genial! Estoy aquí para lo que necesites.",
    ],
    "en": [
        "You're welcome! Let me know if you need anything else.",
        "Perfect! Don't hesitate to ask if anything comes up.",
        "Great! I'm here if you need anything.",
    ],
    "fr": [
        "De rien ! N'hésitez pas si vous avez besoin de quoi que ce soit.",
        "Parfait ! Je suis là si vous avez besoin.",
    ],
    "pt": [
        "De nada! Se precisar de algo mais, estou aqui.",
        "Perfeito! Não hesite em perguntar.",
    ],
}

_MIN_AI_RESPONSE_LENGTH = 80
_MAX_CONFIRMATION_LENGTH = 40


def check_quick_response(
    message: str,
    last_ai_content: str | None,
    guest_language: str = "es",
) -> str | None:
    """Returns a quick ack response if the guest is just confirming, None otherwise."""
    if not last_ai_content:
        return None

    # Last AI response must be substantial
    if len(last_ai_content) < _MIN_AI_RESPONSE_LENGTH:
        return None

    # Guest message must be short
    clean = message.strip().lower().rstrip("!.?¡¿")
    if len(clean) > _MAX_CONFIRMATION_LENGTH:
        return None

    # Check if it's a confirmation word/phrase
    if clean not in _CONFIRMATION_WORDS:
        # Also check if the message starts with a confirmation word
        if not any(clean.startswith(w) for w in _CONFIRMATION_WORDS):
            return None

    # Pick a random ack in the guest's language
    lang = guest_language[:2] if guest_language else "es"
    responses = _ACK_RESPONSES.get(lang, _ACK_RESPONSES["es"])
    return random.choice(responses)
