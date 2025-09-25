import os
from fastmcp import FastMCP
from core.language import enforce_language, detect_language
from core.utils_prompt import load_prompt
from core.mcp_client import mcp_client
from dotenv import load_dotenv

load_dotenv()
interno_prompt = load_prompt("interno_prompt.txt")

mcp = FastMCP("InternoAgent")


@mcp.tool()
async def consulta_encargado(mensaje: str) -> str:
    """Reenvía la consulta al encargado humano vía MCP y ajusta idioma/tono."""
    try:
        lang = detect_language(mensaje)

        tools = await mcp_client.get_tools(server_name="InternoAgent")
        tool = next(t for t in tools if t.name == "consulta_encargado")

        raw_reply = await tool.ainvoke({"mensaje": mensaje})

        return enforce_language(mensaje, raw_reply, lang)

    except Exception as e:
        return f"⚠️ Error en InternoAgent: {e}"


if __name__ == "__main__":
    print("✅ InternoAgent conectado al ENDPOINT_MCP")
    mcp.run(transport="stdio", show_banner=False)
