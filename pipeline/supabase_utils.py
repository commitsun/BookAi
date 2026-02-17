import os
import hashlib
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
    function_name = f"match_documents_{hotel_id.lower().replace(' ', '_')}"
    print(f"🧱 Verificando tabla: {table_name}")

    ensure_pgvector_enabled()

    # Los identificadores de Postgres no pueden superar 63 caracteres.
    # Si el id del hotel es largo, recortamos y añadimos hash para mantener unicidad.
    if len(function_name) > 63:
        digest = hashlib.md5(function_name.encode("utf-8")).hexdigest()[:8]
        function_name = f"{function_name[:54]}_{digest}"

    idx_embedding = f"{table_name}_embedding_ivfflat_idx"
    idx_metadata = f"{table_name}_metadata_gin_idx"
    idx_source_key = f"{table_name}_source_key_idx"

    if len(idx_embedding) > 63:
        idx_embedding = f"{idx_embedding[:54]}_{hashlib.md5(idx_embedding.encode('utf-8')).hexdigest()[:8]}"
    if len(idx_metadata) > 63:
        idx_metadata = f"{idx_metadata[:54]}_{hashlib.md5(idx_metadata.encode('utf-8')).hexdigest()[:8]}"
    if len(idx_source_key) > 63:
        idx_source_key = f"{idx_source_key[:54]}_{hashlib.md5(idx_source_key.encode('utf-8')).hexdigest()[:8]}"

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

        CREATE INDEX IF NOT EXISTS {idx_embedding}
            ON public.{table_name} USING ivfflat (embedding vector_cosine_ops)
            WITH (lists = 100);

        CREATE INDEX IF NOT EXISTS {idx_metadata}
            ON public.{table_name} USING GIN (metadata);

        CREATE INDEX IF NOT EXISTS {idx_source_key}
            ON public.{table_name} ((metadata->>'source_key'));

    CREATE OR REPLACE FUNCTION public.{function_name}(
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

    CREATE OR REPLACE FUNCTION public.match_documents_by_table(
        target_table TEXT,
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
    LANGUAGE plpgsql STABLE AS $$
    BEGIN
        RETURN QUERY EXECUTE format(
            'SELECT id, content, metadata, 1 - (embedding <=> $1) AS similarity
             FROM public.%I
             WHERE ($2 IS NULL OR metadata @> $2)
               AND embedding IS NOT NULL
             ORDER BY embedding <=> $1
             LIMIT $3',
            target_table
        )
        USING query_embedding, filter, match_count;
    END;
    $$;
    """

    try:
        supabase.rpc("exec_sql", {"sql": ddl}).execute()
        print(f"✅ Tabla y función configuradas para {table_name}.")
        print(f"🔎 Función de búsqueda: public.{function_name}(filter, match_count, query_embedding)")
        print("🔎 Función global: public.match_documents_by_table(target_table, filter, match_count, query_embedding)")
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
