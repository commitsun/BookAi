from langchain_openai import OpenAIEmbeddings
from langchain_community.vectorstores import SupabaseVectorStore
from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
embeddings = OpenAIEmbeddings(model="text-embedding-3-small")


def save_chunks_to_supabase(table_name: str, doc_name: str, chunks: list[str]):
    """Guarda chunks en una tabla KB específica."""
    from uuid import uuid4
    rows = []
    for i, chunk in enumerate(chunks):
        rows.append({
            "id": str(uuid4()),
            "content": chunk,
            "metadata": {"source": doc_name, "chunk": i}
        })

    supabase.table(table_name).insert(rows).execute()
    print(f"✅ Insertados {len(rows)} chunks en {table_name}")
