import os
from fastmcp import FastMCP
from langchain_openai import ChatOpenAI
from core.utils_prompt import load_prompt
from core.language import enforce_language, detect_language
from utils.logging_config import silence_logs
from dotenv import load_dotenv

load_dotenv()
silence_logs()

interno_prompt = load_prompt("interno_prompt.txt")

api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise RuntimeError("❌ Falta la variable OPENAI_API_KEY en el entorno.")

llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.3, api_key=api_key)
mcp = FastMCP("InternoAgent")


@mcp.tool()
def consulta_encargado(mensaje: str) -> str:
    """Consulta con el encargado humano cuando no hay respuesta de otros agentes."""
    try:
        lang = detect_language(mensaje)
        response = llm.invoke([
            {"role": "system", "content": interno_prompt},
            {"role": "user", "content": mensaje},
        ])
        return enforce_language(mensaje, response.content, lang)
    except Exception as e:
        return f"⚠️ Error en InternoAgent: {e}"


if __name__ == "__main__":
    print("✅ InternoAgent arrancado con tool: consulta_encargado")
    mcp.run(transport="stdio", show_banner=False)
