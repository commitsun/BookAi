"""
Audio transcription using OpenAI Whisper API.
Returns transcribed text and duration in seconds for cost tracking.
"""

import logging
import os

import httpx

log = logging.getLogger("transcription_service")

WHISPER_URL = "https://api.openai.com/v1/audio/transcriptions"


def _estimate_duration_ogg(file_path: str) -> float | None:
    """Rough duration estimate for OGG files from file size (~16kbps for voice)."""
    try:
        size = os.path.getsize(file_path)
        return size / 2000  # ~16kbps bitrate typical for WhatsApp voice
    except Exception:
        return None


async def transcribe_audio(
    http: httpx.AsyncClient,
    api_key: str,
    file_path: str,
    language: str | None = None,
) -> tuple[str | None, float]:
    """Transcribe an audio file using OpenAI Whisper.

    Returns (text, duration_seconds). Duration is estimated from file size.
    """
    if not os.path.exists(file_path):
        log.warning("Audio file not found: %s", file_path)
        return None, 0.0

    duration = _estimate_duration_ogg(file_path) or 0.0
    filename = os.path.basename(file_path)
    headers = {"Authorization": f"Bearer {api_key}"}

    data = {"model": "whisper-1"}
    if language:
        data["language"] = language

    try:
        with open(file_path, "rb") as f:
            files = {"file": (filename, f, "audio/ogg")}
            r = await http.post(
                WHISPER_URL,
                headers=headers,
                data=data,
                files=files,
                timeout=60,
            )

        if r.status_code != 200:
            log.error("Whisper API error %d: %s", r.status_code, r.text[:200])
            return None, duration

        result = r.json()
        text = result.get("text", "").strip()
        # Whisper sometimes returns duration in the response
        if "duration" in result:
            duration = float(result["duration"])
        log.info("Audio transcribed: %d chars, %.1fs from %s", len(text), duration, filename)
        return (text if text else None), duration

    except Exception as exc:
        log.error("Transcription failed for %s: %s", file_path, exc)
        return None, duration
