import os
from supabase import create_client
from dotenv import load_dotenv

# ===============================================================
# 🌍 Cargar variables de entorno
# ===============================================================
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("❌ Faltan variables SUPABASE_URL o SUPABASE_KEY.")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


# ===============================================================
# 🧩 Verificar extensión pgvector
# ===============================================================
def ensure_pgvector_enabled():
    """Verifica que la extensión pgvector esté disponible en Supabase."""
    print("🧩 Verificando extensión pgvector...")
    try:
        supabase.rpc("execute_sql", {"sql": "SELECT 'vector'::regtype;"}).execute()
        print("✅ Extensión pgvector ya disponible.")
    except Exception as e:
        print(f"⚠️ No se pudo verificar pgvector: {e}")
        print("   👉 Si no está activada, ejecuta en Supabase:")
        print("      CREATE EXTENSION IF NOT EXISTS vector;")


# ===============================================================
# 🧱 Crear tabla KB de hotel (usa función SQL del servidor)
# ===============================================================
def ensure_kb_table_exists(hotel_id: str):
    """
    Crea la tabla específica del hotel (p.ej. kb_alda_ponferrada)
    y la función de búsqueda vectorial asociada.
    """
    table_name = f"kb_{hotel_id.lower().replace(' ', '_')}"
    print(f"🧱 Verificando tabla: {table_name}")

    ensure_pgvector_enabled()

    ddl = f"""
        CREATE EXTENSION IF NOT EXISTS pgcrypto;
        CREATE EXTENSION IF NOT EXISTS vector;

        CREATE TABLE IF NOT EXISTS public.{table_name} (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            content TEXT,
            embedding VECTOR(1536),
            metadata JSONB,
            position INT,  -- 👈 NUEVA COLUMNA
            created_at TIMESTAMPTZ DEFAULT now()
        );

    CREATE OR REPLACE FUNCTION public.match_documents(
        filter JSONB,
        match_count INT,
        query_embedding VECTOR
    )
    RETURNS TABLE (
        id UUID,
        content TEXT,
        metadata JSONB,
        similarity FLOAT
    )
    LANGUAGE SQL STABLE AS $$
        SELECT
            id,
            content,
            metadata,
            1 - ({table_name}.embedding <=> query_embedding) AS similarity
        FROM public.{table_name}
        WHERE
            (filter IS NULL OR {table_name}.metadata @> filter)
            AND embedding IS NOT NULL
        ORDER BY {table_name}.embedding <=> query_embedding
        LIMIT match_count;
    $$;
    """

    try:
        supabase.rpc("exec_sql", {"sql": ddl}).execute()
        print(f"✅ Tabla y función configuradas para {table_name}.")
    except Exception as e:
        print(f"⚠️ Error creando estructura {table_name}: {e}")


# ===============================================================
# 📋 Listar todas las tablas KB creadas
# ===============================================================
def list_existing_kb_tables():
    """Devuelve una lista con todas las tablas KB existentes."""
    try:
        response = supabase.rpc("list_kb_tables").execute()
        tables = [r["table_name"] for r in response.data] if response.data else []
        print(f"📚 Tablas existentes: {tables}")
        return tables
    except Exception as e:
        print(f"⚠️ Error listando tablas KB: {e}")
        return []
