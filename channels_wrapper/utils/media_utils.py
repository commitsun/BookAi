import logging
import requests
from io import BytesIO
from openai import OpenAI

log = logging.getLogger("media_utils")


def download_media_bytes(media_id: str, token: str) -> BytesIO | None:
    """
    Descarga un archivo de audio de WhatsApp (OGG/OPUS) desde Meta Graph API.
    Devuelve un objeto BytesIO con los datos binarios.
    """
    try:
        # 1️⃣ Obtener la URL del media
        url_info = f"https://graph.facebook.com/v19.0/{media_id}"
        headers = {"Authorization": f"Bearer {token}"}
        resp_info = requests.get(url_info, headers=headers, timeout=15)

        if resp_info.status_code != 200:
            log.error(f"❌ Error obteniendo URL de media ({resp_info.status_code}): {resp_info.text}")
            return None

        media_url = resp_info.json().get("url")
        if not media_url:
            log.error("⚠️ No se encontró campo 'url' en respuesta de Meta Graph API.")
            return None

        # 2️⃣ Descargar el archivo binario real
        resp_audio = requests.get(media_url, headers=headers, timeout=30)
        if resp_audio.status_code != 200:
            log.error(f"❌ Error descargando audio ({resp_audio.status_code}): {resp_audio.text}")
            return None

        content_length = len(resp_audio.content)
        log.info(f"✅ Audio descargado correctamente ({content_length} bytes).")
        return BytesIO(resp_audio.content)

    except Exception as e:
        log.error(f"⚠️ Error descargando media: {e}", exc_info=True)
        return None


def transcribe_audio(media_id: str, token: str, openai_key: str) -> str:
    """
    Descarga y transcribe un audio de WhatsApp usando Whisper (modelo whisper-1).
    Retorna el texto transcrito o un mensaje de error.
    """
    try:
        audio_bytes = download_media_bytes(media_id, token)
        if not audio_bytes:
            return "[Error: no se pudo descargar el audio]"

        # Inicializar cliente OpenAI
        client = OpenAI(api_key=openai_key)

        # Whisper requiere un archivo-like (tuple con nombre y tipo MIME)
        transcript = client.audio.transcriptions.create(
            model="whisper-1",
            file=("audio.ogg", audio_bytes, "audio/ogg"),
            prompt="Transcribe de forma clara y precisa la voz de un cliente de hotel en español."
        )

        text = transcript.text.strip()
        log.info(f"📝 Transcripción completada: {text}")
        return text or "[Audio vacío]"

    except Exception as e:
        log.error(f"⚠️ Error al transcribir con Whisper: {e}", exc_info=True)
        return "[Error al transcribir el audio]"
