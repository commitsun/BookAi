from fastmcp import FastMCP
from core.language import enforce_language, detect_language
from core.message_composition.utils_prompt import load_prompt
from dotenv import load_dotenv

load_dotenv()

interno_prompt = load_prompt("interno_prompt.txt")
mcp = FastMCP("InternoAgent")

@mcp.tool()
async def consulta_encargado(mensaje: str) -> str:
    """
    El InternoAgent nunca inventa datos.
    Si no hay información, responde siempre con la frase estándar.
    """
    try:
        lang = detect_language(mensaje)
        return enforce_language(mensaje, "No dispongo de ese dato en este momento.", lang)
    except Exception as e:
        return f"⚠️ Error en InternoAgent: {e}"

if __name__ == "__main__":
    print("✅ InternoAgent en modo seguro listo")
    mcp.run(transport="stdio", show_banner=False)
