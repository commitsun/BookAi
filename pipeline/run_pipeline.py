import os

from pipeline.s3_client import init_hotels_in_supabase, list_hotel_folders
from pipeline.supabase_utils import build_kb_table_name
from pipeline.vectorizer import vectorize_hotel_docs

def main():
    print("🚀 Iniciando pipeline de vectorización...\n")

    full_refresh = os.getenv("FULL_REFRESH", "").lower() in {"1", "true", "yes"}
    if full_refresh:
        print("🧹 FULL_REFRESH activo: se limpiarán tablas antes de vectorizar.")

    # 1️⃣ Crear tablas en Supabase según las carpetas de S3
    _, failed_tables = init_hotels_in_supabase()

    # 2️⃣ Obtener lista de carpetas (hoteles)
    hotels = list_hotel_folders()
    if not hotels:
        print("⚠️ No se encontraron hoteles en S3, pipeline finalizado.")
        return

    failed_table_names = {table_name for _, table_name in failed_tables}

    # 3️⃣ Vectorizar los documentos de cada hotel
    for hotel_folder in hotels:
        table_name = build_kb_table_name(os.path.basename(hotel_folder))
        if table_name in failed_table_names:
            print(f"⏭️  Se omite {hotel_folder}: la tabla {table_name} no pudo prepararse en Supabase.")
            continue
        try:
            vectorize_hotel_docs(hotel_folder, full_refresh=full_refresh)
        except Exception as e:
            print(f"⚠️ Error vectorizando {hotel_folder}: {e}")

    if failed_tables:
        print("\n⚠️ Pipeline completado con incidencias: faltan permisos o DDL para algunas tablas KB.")
    else:
        print("\n🎉 Pipeline completado con éxito ✅")


if __name__ == "__main__":
    main()
