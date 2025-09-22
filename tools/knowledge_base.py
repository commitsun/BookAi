import os
import json
from pathlib import Path
from fastmcp import FastMCP
from dotenv import load_dotenv
from utils.logging_config import silence_logs
from langchain_openai import ChatOpenAI

# =========
# Setup
# =========
load_dotenv()
silence_logs()

mcp = FastMCP("KnowledgeBase")

# Ruta del archivo JSON con la base de conocimientos
KB_PATH = Path("tools/knowledge_base.json")
if not KB_PATH.exists():
    raise FileNotFoundError(f"❌ No se encontró {KB_PATH}. Crea el JSON con los datos del hotel.")

with open(KB_PATH, encoding="utf-8") as f:
    KNOWLEDGE = json.load(f)

# LLM
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise RuntimeError("❌ Falta la variable OPENAI_API_KEY en el entorno.")

llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.2, api_key=api_key)


# =========
# Tool expuesta por MCP
# =========
@mcp.tool()
def consulta_kb(clave: str) -> str:
    """
    Consulta la base de conocimientos usando IA para interpretar la pregunta.
    """
    try:
        # Prompt: damos el JSON como contexto
        system_prompt = (
            "Eres un sistema de soporte que responde preguntas sobre un hotel "
            "usando exclusivamente la información proporcionada en la base de conocimientos JSON. "
            "No inventes nada fuera de lo que esté en el JSON. "
            "Si no encuentras la información, responde: "
            "'Lo siento, no dispongo de ese dato en este momento.'\n\n"
            f"Base de conocimientos:\n{json.dumps(KNOWLEDGE, ensure_ascii=False, indent=2)}"
        )

        response = llm.invoke([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": clave},
        ])

        return response.content.strip()

    except Exception as e:
        return f"⚠️ Error en KnowledgeBase: {e}"


# =========
# Run
# =========
if __name__ == "__main__":
    print("✅ KnowledgeBaseAgent arrancado con tool: consulta_kb")
    mcp.run(transport="stdio", show_banner=False)
