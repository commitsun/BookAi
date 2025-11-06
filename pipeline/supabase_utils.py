import os
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("❌ Faltan variables SUPABASE_URL o SUPABASE_KEY.")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def ensure_kb_table_exists(hotel_id: str):
    """
    Crea una tabla de base de conocimiento (knowledge base) en Supabase
    si aún no existe. Ejemplo: kb_alda_ponferrada
    """
    table_name = f"kb_{hotel_id.lower()}"

    print(f"🧱 Verificando tabla: {table_name}")

    ddl = f"""
    create table if not exists {table_name} (
        id uuid primary key default gen_random_uuid(),
        content text,
        embedding vector(1536),
        metadata jsonb,
        created_at timestamp with time zone default now()
    );
    """

    try:
        supabase.rpc("exec_sql", {"sql": ddl}).execute()
        print(f"✅ Tabla {table_name} creada o existente.")
    except Exception as e:
        print(f"⚠️ Error creando {table_name}: {e}")
