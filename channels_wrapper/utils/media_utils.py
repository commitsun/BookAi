import requests
from io import BytesIO
from openai import OpenAI


def download_media_bytes(media_id: str, token: str) -> BytesIO | None:
    """
    Descarga el audio de WhatsApp (OGG/OPUS) directamente desde la API de Meta.
    Devuelve BytesIO con los datos binarios.
    """
    try:
        url = f"https://graph.facebook.com/v19.0/{media_id}"
        headers = {"Authorization": f"Bearer {token}"}
        r = requests.get(url, headers=headers)

        if r.status_code != 200:
            print(f"❌ Error obteniendo URL del media: {r.text}")
            return None

        media_url = r.json().get("url")
        if not media_url:
            print("❌ No se encontró la URL del media en la respuesta.")
            return None

        # Descargar el archivo .ogg
        r = requests.get(media_url, headers=headers)
        if r.status_code == 200:
            print(f"✅ Audio descargado correctamente ({len(r.content)} bytes)")
            return BytesIO(r.content)

        print(f"❌ Error descargando el archivo: {r.text}")
        return None

    except Exception as e:
        print(f"⚠️ Error descargando media: {e}")
        return None


def transcribe_audio(media_id: str, token: str, openai_key: str) -> str:
    """
    Descarga y transcribe el audio de WhatsApp usando Whisper (sin conversión).
    """
    try:
        audio_bytes = download_media_bytes(media_id, token)
        if not audio_bytes:
            return "[Error: no se pudo descargar el audio]"

        client = OpenAI(api_key=openai_key)

        # Enviar el archivo directamente a Whisper
        transcript = client.audio.transcriptions.create(
            model="whisper-1",
            file=("audio.ogg", audio_bytes, "audio/ogg"),
            prompt="Transcribe de forma clara y precisa la voz de un cliente sobre un hotel."
        )

        text = transcript.text.strip()
        print(f"📝 Transcripción: {text}")
        return text or "[Audio vacío]"

    except Exception as e:
        print(f"⚠️ Error al transcribir con Whisper: {e}")
        return "[Error al transcribir el audio]"
