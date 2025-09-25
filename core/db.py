import os
from supabase import create_client
from langchain_openai import OpenAIEmbeddings

# =========
# Conexión a Supabase
# =========
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# =========
# Embeddings
# =========
embeddings = OpenAIEmbeddings(model="text-embedding-3-small")

# =========
# Guardar mensajes en Supabase
# =========
def save_message(conversation_id: str, role: str, content: str):
    """
    Guarda un mensaje en Supabase con su embedding.
    - conversation_id: ID artificial generado al inicio de la conversación
    - role: "user" o "assistant"
    - content: texto del mensaje
    """
    try:
        # 1. Generar embedding del mensaje
        embedding_vector = embeddings.embed_query(content)

        # 2. Insertar manualmente en Supabase
        supabase.table("chat_history").insert({
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
            "metadata": {"conversation_id": conversation_id, "role": role},
            "embedding": embedding_vector
        }).execute()

    except Exception as e:
        print(f"⚠️ Error guardando mensaje en Supabase: {e}")
