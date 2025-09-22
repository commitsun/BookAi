from fastmcp import FastMCP
from langchain_openai import ChatOpenAI
from utils.logging_config import silence_logs
from pathlib import Path
from dotenv import load_dotenv
import os

# Cargar variables del .env
load_dotenv()

silence_logs()
mcp = FastMCP("InfoAgent")

def load_prompt(filename: str) -> str:
    return (Path("prompts") / filename).read_text(encoding="utf-8")

info_prompt = load_prompt("info_prompt.txt")

# API Key obligatoria
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise RuntimeError("❌ Falta la variable OPENAI_API_KEY en el entorno.")

llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.3, api_key=api_key)

@mcp.tool()
def consulta_info(pregunta: str) -> str:
    """Responde preguntas generales sobre el hotel"""
    response = llm.invoke([
        {"role": "system", "content": info_prompt},
        {"role": "user", "content": pregunta}
    ])
    return response.content

if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
