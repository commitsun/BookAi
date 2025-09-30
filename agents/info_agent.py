import os
from fastmcp import FastMCP
from core.language import enforce_language, detect_language
from core.message_composition.utils_prompt import load_prompt
from dotenv import load_dotenv
from langchain_mcp_adapters.client import MCPClient

load_dotenv()

info_prompt = load_prompt("info_prompt.txt")
mcp = FastMCP("InfoAgent")

# Cliente MCP directo a la KB
mcp_url = os.getenv("ENDPOINT_MCP")
kb_client = MCPClient(transport="streamable_http", url=mcp_url)

@mcp.tool()
async def consulta_info(pregunta: str) -> str:
    """Consulta información general en la Base de Conocimientos del hotel."""
    try:
        lang = detect_language(pregunta)
        tool = await kb_client.get_tool("Base_de_conocimientos_del_hotel")
        raw_reply = await tool.ainvoke({"input": pregunta})
        return enforce_language(pregunta, raw_reply, lang)
    except Exception as e:
        return f"⚠️ Error en InfoAgent: {e}"

if __name__ == "__main__":
    print("✅ InfoAgent conectado directamente a la KB")
    mcp.run(transport="stdio", show_banner=False)
