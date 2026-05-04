"""
Detect the language of a guest message using langdetect.

Only attempts detection on messages with enough content (>= 10 chars).
Returns None for short messages — the caller should keep session.guest_language
as null until a longer message arrives.

Uses detect_langs() with probabilities and filters to supported languages
to avoid false positives (e.g. langdetect confuses es/ca on short texts).
"""

from langdetect import detect_langs, DetectorFactory, LangDetectException

# Make detection deterministic
DetectorFactory.seed = 0

_MIN_LENGTH = 10
_MIN_CONFIDENCE = 0.5

SUPPORTED_LANGUAGES = {"es", "en", "fr", "pt", "gl"}

LANGUAGE_NAMES = {
    "es": "Spanish",
    "en": "English",
    "fr": "French",
    "pt": "Portuguese",
    "gl": "Galician",
}


def detect_language(text: str) -> str | None:
    """Detect language from text. Returns 2-letter code or None."""
    if not text or len(text.strip()) < _MIN_LENGTH:
        return None

    try:
        results = detect_langs(text)
    except LangDetectException:
        return None

    # Pick the best supported language above confidence threshold
    for result in results:
        code = result.lang[:2]
        if code in SUPPORTED_LANGUAGES and result.prob >= _MIN_CONFIDENCE:
            return code

    return None
