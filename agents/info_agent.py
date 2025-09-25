import os
from fastmcp import FastMCP
from core.language import enforce_language, detect_language
from core.utils_prompt import load_prompt
from core.mcp_client import mcp_client
from dotenv import load_dotenv

load_dotenv()
info_prompt = load_prompt("info_prompt.txt")

mcp = FastMCP("InfoAgent")


@mcp.tool()
async def consulta_info(pregunta: str) -> str:
    """Consulta información general al endpoint MCP y ajusta idioma/tono."""
    try:
        lang = detect_language(pregunta)

        tools = await mcp_client.get_tools(server_name="InfoAgent")
        tool = next(t for t in tools if t.name == "consulta_info")

        raw_reply = await tool.ainvoke({"pregunta": pregunta})

        return enforce_language(pregunta, raw_reply, lang)

    except Exception as e:
        return f"⚠️ Error en InfoAgent: {e}"


if __name__ == "__main__":
    print("✅ InfoAgent conectado al ENDPOINT_MCP")
    mcp.run(transport="stdio", show_banner=False)
