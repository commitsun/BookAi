import os
from fastmcp import FastMCP
from langchain_openai import ChatOpenAI
from pathlib import Path
from utils.logging_config import silence_logs
from dotenv import load_dotenv
load_dotenv()

silence_logs()

def load_prompt(filename: str) -> str:
    return (Path("prompts") / filename).read_text(encoding="utf-8")

interno_prompt = load_prompt("interno_prompt.txt")

api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise RuntimeError("❌ Falta la variable OPENAI_API_KEY en el entorno.")

llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.3, api_key=api_key)
mcp = FastMCP("InternoAgent")

def build_prompt(prompt_text: str, user_message: str):
    return [
        {"role": "system", "content": prompt_text},
        {
            "role": "system",
            "content": "⚠️ Detecta automáticamente el idioma del usuario y responde SIEMPRE en ese idioma."
        },
        {"role": "user", "content": user_message},
    ]

@mcp.tool()
def consulta_encargado(mensaje: str) -> str:
    """Consulta con el encargado humano cuando no hay respuesta de otros agentes"""
    messages = build_prompt(interno_prompt, mensaje)
    response = llm.invoke(messages)
    return response.content

if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
