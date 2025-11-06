from pipeline.s3_client import init_hotels_in_supabase

def main():
    print("🚀 Iniciando pipeline de vectorización...\n")
    init_hotels_in_supabase()
    print("\n🎉 Pipeline completado con éxito.")

if __name__ == "__main__":
    main()
