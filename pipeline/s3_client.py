import os
import boto3
from typing import List
from .supabase_utils import ensure_kb_table_exists

AWS_REGION = os.getenv("AWS_DEFAULT_REGION", "eu-west-1")
S3_BUCKET = os.getenv("S3_BUCKET", "bookai-pre-roomdoo")

s3 = boto3.client(
    "s3",
    region_name=AWS_REGION,
    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
)


def list_hotel_folders(prefix: str = "") -> List[str]:
    """Lista las carpetas raíz (hoteles) del bucket S3."""
    print(f"📦 Listando carpetas raíz en bucket: {S3_BUCKET} ...")

    response = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix, Delimiter="/")
    if "CommonPrefixes" not in response:
        print("⚠️ No se encontraron carpetas en el bucket.")
        return []

    folders = [p["Prefix"].rstrip("/") for p in response["CommonPrefixes"]]
    print(f"🏨 Carpetas detectadas: {', '.join(folders)}")
    return folders


def init_hotels_in_supabase():
    """Detecta hoteles y crea sus tablas KB."""
    hotels = list_hotel_folders()
    if not hotels:
        print("⚠️ No hay carpetas que procesar.")
        return

    for hotel_folder in hotels:
        hotel_id = os.path.basename(hotel_folder)
        print(f"\n🔍 Procesando hotel: {hotel_id}")
        ensure_kb_table_exists(hotel_id)

    print("\n✅ Tablas KB verificadas correctamente.")


if __name__ == "__main__":
    init_hotels_in_supabase()
