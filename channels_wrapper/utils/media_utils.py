import requests
from io import BytesIO
from openai import OpenAI

# ------------------------------------------------------------------
# 🎧 Descarga y transcripción de audio
# ------------------------------------------------------------------
def download_media_bytes(media_id: str, token: str) -> BytesIO | None:
    """
    Descarga un archivo multimedia (audio, imagen, etc.) desde la API de Meta.
    Devuelve el contenido como BytesIO o None si hay error.
    """
    try:
        url = f"https://graph.facebook.com/v19.0/{media_id}"
        headers = {"Authorization": f"Bearer {token}"}
        r = requests.get(url, headers=headers)
        if r.status_code != 200:
            print(f"❌ Error obteniendo URL de media {media_id}: {r.text}")
            return None

        media_url = r.json().get("url")
        if not media_url:
            print("❌ No se encontró URL de media.")
            return None

        r = requests.get(media_url, headers=headers)
        if r.status_code == 200:
            return BytesIO(r.content)

        print(f"❌ Error descargando media {media_id}: {r.text}")
        return None

    except Exception as e:
        print("⚠️ Error al descargar media:", e)
        return None


def transcribe_audio(media_id: str, token: str, openai_key: str) -> str:
    """
    Descarga y transcribe un audio de WhatsApp usando OpenAI Whisper.
    """
    try:
        audio_bytes = download_media_bytes(media_id, token)
        if not audio_bytes:
            return "[Error: no se pudo descargar el audio]"

        client = OpenAI(api_key=openai_key)
        transcript = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_bytes,
            prompt="Pregunta de un cliente sobre un hotel. Transcribe lo más claro posible."
        )
        text = transcript.text.strip()
        print(f"📝 Transcripción: {text}")
        return text or "[Audio vacío]"

    except Exception as e:
        print("⚠️ Error al transcribir audio:", e)
        return "[Error al transcribir el audio]"
