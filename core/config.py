# core/config.py

import os
from supabase import create_client
from openai import OpenAI
from dotenv import load_dotenv

# Cargar variables de .env (útil en local)
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("❌ Falta configuración de Supabase en variables de entorno")

if not OPENAI_API_KEY:
    raise RuntimeError("❌ Falta OPENAI_API_KEY en variables de entorno")

# Cliente Supabase
supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Cliente OpenAI
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# Modelo de embeddings (igual que en n8n)
MODEL_EMBEDDING = "text-embedding-3-small"

# Tabla de knowledge base actual (un hotel)
DEFAULT_KB_TABLE = "kb_alda_ponferrada"
