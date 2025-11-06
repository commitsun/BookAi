import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "hotel_docs")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("❌ Faltan SUPABASE_URL o SUPABASE_KEY en el entorno.")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def list_docs(prefix: str = "") -> list[str]:
    """Lista archivos (no carpetas) del bucket."""
    files = supabase.storage.from_(SUPABASE_BUCKET).list(path=prefix)
    return [f["name"] for f in files if not f.get("metadata", {}).get("isDirectory")]


def download_doc(filename: str) -> bytes:
    """Descarga el archivo más reciente del bucket."""
    return supabase.storage.from_(SUPABASE_BUCKET).download(filename)
