import os
import boto3
from typing import List
from .supabase_utils import ensure_kb_table_exists


# ===============================
# 🔧 Configuración básica
# ===============================
AWS_REGION = os.getenv("AWS_DEFAULT_REGION", "eu-west-1")
S3_BUCKET = os.getenv("S3_BUCKET", "bookai-pre-roomdoo")


# ===============================
# 🔐 Cliente S3 compatible OIDC
# ===============================
def get_s3_client():
    """
    Crea un cliente S3 compatible tanto con OIDC (GitHub Actions)
    como con entornos locales configurados con `aws configure`.

    No fuerza credenciales estáticas para evitar el error:
    'InvalidAccessKeyId' al usar OIDC.
    """
    session = boto3.Session(region_name=AWS_REGION)
    return session.client("s3")


s3 = get_s3_client()


# ===============================
# 📂 Gestión de carpetas (hoteles)
# ===============================
def list_hotel_folders(prefix: str = "") -> List[str]:
    """
    Lista las carpetas raíz (hoteles) dentro del bucket S3.
    Cada carpeta representa una base de conocimiento separada.
    """
    print(f"📦 Listando carpetas raíz en bucket: {S3_BUCKET} ...")

    # Comprobar que el bucket es accesible
    try:
        s3.head_bucket(Bucket=S3_BUCKET)
    except Exception as e:
        print(f"❌ No se puede acceder al bucket '{S3_BUCKET}': {e}")
        return []

    # Listar carpetas raíz usando el delimitador "/"
    response = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix, Delimiter="/")

    if "CommonPrefixes" not in response:
        print("⚠️ No se encontraron carpetas en el bucket.")
        return []

    folders = [p["Prefix"].rstrip("/") for p in response["CommonPrefixes"]]
    print(f"🏨 Carpetas detectadas: {', '.join(folders)}")
    return folders


# ===============================
# 🧠 Inicialización en Supabase
# ===============================
def init_hotels_in_supabase():
    """
    Detecta las carpetas de hoteles en S3 y asegura
    que cada una tenga su tabla de embeddings en Supabase.
    """
    hotels = list_hotel_folders()
    if not hotels:
        print("⚠️ No hay carpetas que procesar.")
        return

    for hotel_folder in hotels:
        hotel_id = os.path.basename(hotel_folder)
        print(f"\n🔍 Procesando hotel: {hotel_id}")
        ensure_kb_table_exists(hotel_id)

    print("\n✅ Tablas KB verificadas correctamente.")


# ===============================
# ▶️ Ejecución directa (CLI)
# ===============================
if __name__ == "__main__":
    init_hotels_in_supabase()
