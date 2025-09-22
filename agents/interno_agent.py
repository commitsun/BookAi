from fastmcp import FastMCP
from langchain_openai import ChatOpenAI
from utils.logging_config import silence_logs
from pathlib import Path
from dotenv import load_dotenv
import os

# Cargar variables del .env
load_dotenv()

silence_logs()
mcp = FastMCP("InternoAgent")

def load_prompt(filename: str) -> str:
    return (Path("prompts") / filename).read_text(encoding="utf-8")

interno_prompt = load_prompt("interno_prompt.txt")

# API Key obligatoria
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise RuntimeError("❌ Falta la variable OPENAI_API_KEY en el entorno.")

llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.3, api_key=api_key)

@mcp.tool()
def consulta_encargado(mensaje: str) -> str:
    """Traslada la consulta al encargado humano del hotel"""
    response = llm.invoke([
        {"role": "system", "content": interno_prompt},
        {"role": "user", "content": mensaje}
    ])
    return response.content

if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
