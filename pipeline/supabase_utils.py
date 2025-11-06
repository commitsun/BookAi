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
    Crea o verifica la tabla KB de un hotel usando la función SQL remota.
    Requiere que exista la función `ensure_kb_table_exists(hotel_name text)`
    en Supabase.
    """
    table_name = f"kb_{hotel_id.lower()}"
    print(f"🧱 Verificando tabla: {table_name}")

    ensure_pgvector_enabled()

    try:
        supabase.rpc("ensure_kb_table_exists", {"hotel_name": hotel_id}).execute()
        print(f"✅ Tabla {table_name} creada o existente.")
    except Exception as e:
        print(f"⚠️ Error creando {table_name}: {e}")


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
