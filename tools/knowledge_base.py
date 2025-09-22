import os
import json
from fastmcp import FastMCP
from langchain_openai import ChatOpenAI
from utils.logging_config import silence_logs
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()
silence_logs()

mcp = FastMCP("KnowledgeBase")

# 📂 Cargar el JSON de la base de conocimientos
KB_PATH = Path(__file__).parent / "knowledge.json"
if not KB_PATH.exists():
    raise RuntimeError(f"❌ No se encontró el archivo {KB_PATH}")

with open(KB_PATH, "r", encoding="utf-8") as f:
    KNOWLEDGE = json.load(f)

# 🔑 Recuperar la API Key
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise RuntimeError("❌ Falta la variable OPENAI_API_KEY en el entorno.")

# 🔮 LLM para interpretar la KB cuando no hay coincidencia exacta
llm = ChatOpenAI(model="gpt-4o-mini", temperature=0, api_key=api_key)


def search_json(data, query: str):
    """
    Búsqueda superficial por coincidencias de texto en claves y valores del JSON.
    Devuelve el primer match que encuentre.
    """
    q = query.lower()

    if isinstance(data, dict):
        for k, v in data.items():
            if q in str(k).lower() or q in str(v).lower():
                return v
            res = search_json(v, query)
            if res:
                return res
    elif isinstance(data, list):
        for item in data:
            res = search_json(item, query)
            if res:
                return res
    return None


@mcp.tool()
def consulta_kb(clave: str) -> str:
    """
    Busca información en la base de conocimientos del hotel.
    1. Intenta coincidencia directa en el JSON.
    2. Si no encuentra, pregunta al LLM para interpretar el JSON completo.
    """
    try:
        # 1. Intentar búsqueda directa en JSON
        direct_match = search_json(KNOWLEDGE, clave)
        if direct_match:
            return str(direct_match)

        # 2. No hubo match → preguntar al LLM interpretando todo el JSON
        response = llm.invoke([
            {
                "role": "system",
                "content": (
                    "Eres un asistente que responde SÓLO con datos del JSON "
                    "que te paso a continuación. Nunca inventes ni uses placeholders. "
                    "Si el dato no está en el JSON, responde: "
                    "'Lo siento, no dispongo de ese dato en este momento.'\n\n"
                    f"Base de conocimientos:\n{json.dumps(KNOWLEDGE, ensure_ascii=False, indent=2)}"
                )
            },
            {"role": "user", "content": clave},
        ])
        return response.content

    except Exception as e:
        return f"⚠️ Error en KnowledgeBase: {e}"


if __name__ == "__main__":
    print("✅ KnowledgeBaseAgent arrancado con tool: consulta_kb")
    mcp.run(transport="stdio", show_banner=False)
