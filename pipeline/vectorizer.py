import os
from uuid import uuid4
from typing import List
from dotenv import load_dotenv
from supabase import create_client
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitter import RecursiveCharacterTextSplitter
import boto3

# =====================================
# 🔧 Cargar configuración
# =====================================
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
S3_BUCKET = os.getenv("S3_BUCKET", "bookai-pre-roomdoo")
AWS_REGION = os.getenv("AWS_DEFAULT_REGION", "eu-west-1")

# Clientes globales
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
s3 = boto3.client("s3", region_name=AWS_REGION)
embeddings_model = OpenAIEmbeddings(model="text-embedding-3-small")


# =====================================
# 📄 Lectura y chunking de documentos
# =====================================
def load_text_from_s3(key: str) -> str:
    """Descarga y lee el contenido de un archivo de texto plano desde S3."""
    obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
    return obj["Body"].read().decode("utf-8")


def chunk_text(text: str, chunk_size=1000, overlap=100) -> List[str]:
    """Divide el texto en fragmentos (chunks) usando LangChain."""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=overlap,
        separators=["\n\n", "\n", ".", " "],
    )
    return splitter.split_text(text)


# =====================================
# 🧠 Vectorización e inserción en Supabase
# =====================================
def save_chunks_to_supabase(table_name: str, doc_name: str, chunks: List[str]):
    """Vectoriza e inserta los chunks en la tabla del hotel correspondiente."""
    print(f"🧩 Generando embeddings para {doc_name}...")

    # Generar embeddings en bloque
    vectors = embeddings_model.embed_documents(chunks)

    rows = []
    for i, (chunk, vector) in enumerate(zip(chunks, vectors)):
        rows.append({
            "id": str(uuid4()),
            "content": chunk,
            "embedding": vector,
            "metadata": {"source": doc_name, "chunk": i},
        })

    # Insertar en Supabase
    supabase.table(table_name).insert(rows).execute()
    print(f"✅ Insertados {len(rows)} chunks en {table_name}")


# =====================================
# 🚀 Función principal
# =====================================
def vectorize_hotel_docs(hotel_folder: str):
    """
    Descarga los documentos de un hotel desde S3,
    los divide en chunks, genera embeddings y los guarda en Supabase.
    """
    table_name = f"kb_{os.path.basename(hotel_folder).lower()}"

    print(f"\n🚀 Iniciando vectorización para: {table_name}")
    prefix = f"{hotel_folder}/"

    response = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix)
    if "Contents" not in response:
        print(f"⚠️ No se encontraron archivos en {prefix}")
        return

    for obj in response["Contents"]:
        key = obj["Key"]
        if key.endswith("/"):  # Saltar subcarpetas vacías
            continue

        file_name = os.path.basename(key)
        print(f"📄 Procesando {file_name}...")

        try:
            text = load_text_from_s3(key)
            chunks = chunk_text(text)
            save_chunks_to_supabase(table_name, file_name, chunks)
        except Exception as e:
            print(f"⚠️ Error procesando {file_name}: {e}")

    print(f"🎉 Vectorización completada para {table_name} ✅")


# =====================================
# ▶️ CLI
# =====================================
if __name__ == "__main__":
    # Ejemplo: python -m pipeline.vectorizer Alda_Ponferrada
    import sys
    if len(sys.argv) < 2:
        print("❌ Uso: python -m pipeline.vectorizer <nombre_carpeta_hotel>")
    else:
        vectorize_hotel_docs(sys.argv[1])
