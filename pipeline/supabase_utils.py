import os
from supabase import create_client
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("❌ Faltan variables SUPABASE_URL o SUPABASE_KEY.")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def ensure_pgvector_enabled():
    """
    Verifica que la extensión pgvector está disponible en Supabase.
    Si no lo está, muestra un aviso.
    """
    print("🧩 Verificando extensión pgvector...")
    try:
        supabase.rpc("exec_sql", {"sql": "SELECT 'vector'::regtype;"}).execute()
        print("✅ Extensión pgvector ya disponible.")
    except Exception as e:
        print("⚠️ Extensión pgvector no disponible. Actívala manualmente:")
        print("   👉 CREATE EXTENSION IF NOT EXISTS vector;")


def ensure_kb_table_exists(hotel_id: str):
    """
    Crea una tabla de base de conocimiento (KB) en Supabase si no existe.
    """
    table_name = f"kb_{hotel_id.lower()}"
    print(f"🧱 Verificando tabla: {table_name}")

    ensure_pgvector_enabled()

    ddl = f"""
    CREATE TABLE IF NOT EXISTS {table_name} (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        content TEXT,
        embedding VECTOR(1536),
        metadata JSONB,
        created_at TIMESTAMPTZ DEFAULT now()
    );
    """

    try:
        supabase.rpc("exec_sql", {"sql": ddl}).execute()
        print(f"✅ Tabla {table_name} creada o existente.")
    except Exception as e:
        print(f"⚠️ Error creando {table_name}: {e}")
