import os
from pathlib import Path

REQUIRED_ENV = {
    # ---- SUPABASE ----
    "SUPABASE_URL": "URL de tu proyecto Supabase",
    "SUPABASE_KEY": "Clave service_role de Supabase (no la anon)",
    # ---- OPENAI ----
    "OPENAI_API_KEY": "Tu clave API de OpenAI",
    # ---- AWS ----
    "AWS_DEFAULT_REGION": "Región AWS (ej: eu-west-1)",
    "S3_BUCKET": "Nombre del bucket S3 (ej: bookai-pre-roomdoo)",
}

OPTIONAL_ENV = {
    "SUPABASE_BUCKET": "Bucket opcional en Supabase Storage",
    "AWS_ACCESS_KEY_ID": "Solo necesario si ejecutas el pipeline localmente",
    "AWS_SECRET_ACCESS_KEY": "Solo necesario si ejecutas el pipeline localmente",
}

def check_env():
    print("🔍 Verificando variables de entorno...\n")

    missing = []
    for key, desc in REQUIRED_ENV.items():
        if not os.getenv(key):
            missing.append(f"❌ {key} → falta ({desc})")
        else:
            print(f"✅ {key} = {os.getenv(key)[:8]}...")

    if missing:
        print("\n⚠️  Faltan variables críticas:\n")
        print("\n".join(missing))
        print("\n💡 Añádelas a tu archivo .env o a GitHub Secrets antes de continuar.")
        exit(1)
    else:
        print("\n🎉 Todas las variables requeridas están configuradas correctamente!\n")

    print("ℹ️  Variables opcionales (solo si ejecutas localmente):")
    for key, desc in OPTIONAL_ENV.items():
        if os.getenv(key):
            print(f"   ✅ {key} = presente")
        else:
            print(f"   ⚪ {key} (opcional: {desc})")


if __name__ == "__main__":
    env_path = Path(".env")
    if env_path.exists():
        print(f"📁 Cargando variables desde {env_path.resolve()}")
        from dotenv import load_dotenv
        load_dotenv(env_path)
    else:
        print("⚠️ No se encontró .env, usando variables del entorno del sistema.")
    check_env()
