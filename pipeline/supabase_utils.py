import os
from supabase import create_client
from dotenv import load_dotenv

# Cargar variables de entorno desde .env
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("❌ Faltan variables SUPABASE_URL o SUPABASE_KEY en tu archivo .env.")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def ensure_pgvector_enabled():
    """
    Verifica que la extensión pgvector está disponible en Supabase.
    """
    print("🧩 Verificando extensión pgvector...")
    try:
        supabase.rpc("exec_sql", {"sql": "SELECT 'vector'::regtype;"}).execute()
        print("✅ Extensión pgvector ya disponible.")
    except Exception:
        print("⚠️ Extensión pgvector no disponible. Actívala manualmente:")
        print("   👉 CREATE EXTENSION IF NOT EXISTS vector;")


def setup_documents_schema():
    """
    Crea la tabla 'public.documents' y la función 'match_documents' en Supabase si no existen.
    """
    print("🧱 Verificando estructura de la base de datos (tabla + función)...")

    ensure_pgvector_enabled()

    ddl = """
    -- Crear extensión vector (si no existe)
    CREATE EXTENSION IF NOT EXISTS vector;

    -- Crear tabla documents
    CREATE TABLE IF NOT EXISTS public.documents (
      id BIGSERIAL PRIMARY KEY,
      content TEXT,
      metadata JSONB,
      embedding VECTOR(1536)
    );

    -- Crear función para búsqueda semántica
    CREATE OR REPLACE FUNCTION public.match_documents(
      query_embedding VECTOR(1536),
      match_count INT DEFAULT 5,
      filter JSONB DEFAULT '{}'
    )
    RETURNS TABLE (
      id BIGINT,
      content TEXT,
      metadata JSONB,
      similarity FLOAT
    )
    LANGUAGE plpgsql
    AS $$
    #variable_conflict use_column
    BEGIN
      RETURN QUERY
      SELECT
        d.id,
        d.content,
        d.metadata,
        1 - (d.embedding <=> query_embedding) AS similarity
      FROM public.documents AS d
      WHERE
        (filter = '{}' OR d.metadata @> filter)
        AND d.embedding IS NOT NULL
      ORDER BY d.embedding <=> query_embedding
      LIMIT match_count;
    END;
    $$;
    """

    try:
        supabase.rpc("exec_sql", {"sql": ddl}).execute()
        print("✅ Tabla 'documents' y función 'match_documents' creadas o actualizadas correctamente.")
    except Exception as e:
        print(f"⚠️ Error al crear la estructura: {e}")


if __name__ == "__main__":
    setup_documents_schema()
