"""
Detect the language of a guest message using langdetect.

Only attempts detection on messages with enough content (>= 10 chars).
Returns None for short messages — the caller should keep session.guest_language
as null until a longer message arrives.
"""

from langdetect import detect, DetectorFactory, LangDetectException

# Make detection deterministic
DetectorFactory.seed = 0

_MIN_LENGTH = 10


def detect_language(text: str) -> str | None:
    """Detect language from text. Returns BCP-47 tag or None if too short."""
    if not text or len(text.strip()) < _MIN_LENGTH:
        return None

    try:
        return detect(text)[:2]
    except LangDetectException:
        return None
