import os
from fastmcp import FastMCP
from core.language import enforce_language, detect_language
from core.utils_prompt import load_prompt
from dotenv import load_dotenv

load_dotenv()

interno_prompt = load_prompt("interno_prompt.txt")
mcp = FastMCP("InternoAgent")


@mcp.tool()
async def consulta_encargado(mensaje: str) -> str:
    """
    En la demo, el InternoAgent nunca inventa datos.
    Si no hay información, responde claramente que no dispone de ese dato.
    """
    try:
        lang = detect_language(mensaje)

        # 🚫 No inventamos → siempre devolvemos un aviso neutro
        reply = "No dispongo de ese dato en este momento."

        # ✅ Sanitizar y adaptar idioma
        safe_reply = reply.encode("utf-8", errors="replace").decode("utf-8")
        return enforce_language(mensaje, safe_reply, lang)

    except Exception as e:
        return f"⚠️ Error en InternoAgent: {e}"


if __name__ == "__main__":
    print("✅ InternoAgent (demo seguro) arrancado con tool: consulta_encargado")
    mcp.run(transport="stdio", show_banner=False)
