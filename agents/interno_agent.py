import os
from fastmcp import FastMCP
# from core.language import enforce_language, detect_language
# from utils.utils_prompt import load_prompt
from dotenv import load_dotenv
# from langchain_mcp_adapters.client import MCPClient

load_dotenv()

# info_prompt = load_prompt("info_prompt.txt")
mcp = FastMCP("InternoAgent")

# # Cliente MCP directo a la KB
# mcp_url = os.getenv("ENDPOINT_MCP")
# kb_client = MCPClient(transport="streamable_http", url=mcp_url)

# -------------------------------------------------------------
# 🧩 Tool temporal (placeholder)
# -------------------------------------------------------------
@mcp.tool()
async def interno_placeholder(mensaje: str) -> str:
    """
    Placeholder temporal para el agente interno.
    (La nueva funcionalidad se añadirá aquí más adelante)
    """
    return "🧠 InternoAgent operativo. Esperando nueva funcionalidad."

# -------------------------------------------------------------
# 🚀 Ejecución principal del agente
# -------------------------------------------------------------
if __name__ == "__main__":
    print("✅ InternoAgent iniciado (modo placeholder)")
    mcp.run(transport="stdio", show_banner=False)
