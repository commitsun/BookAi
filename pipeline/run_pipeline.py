from pipeline.s3_client import init_hotels_in_supabase, list_hotel_folders
from pipeline.vectorizer import vectorize_hotel_docs

def main():
    print("🚀 Iniciando pipeline de vectorización...\n")

    # 1️⃣ Crear tablas en Supabase según las carpetas de S3
    init_hotels_in_supabase()

    # 2️⃣ Obtener lista de carpetas (hoteles)
    hotels = list_hotel_folders()
    if not hotels:
        print("⚠️ No se encontraron hoteles en S3, pipeline finalizado.")
        return

    # 3️⃣ Vectorizar los documentos de cada hotel
    for hotel_folder in hotels:
        try:
            vectorize_hotel_docs(hotel_folder)
        except Exception as e:
            print(f"⚠️ Error vectorizando {hotel_folder}: {e}")

    print("\n🎉 Pipeline completado con éxito ✅")


if __name__ == "__main__":
    main()
