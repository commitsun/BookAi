import os
from langchain_mcp_adapters.client import MultiServerMCPClient

mcp_url = os.getenv("ENDPOINT_MCP")
if not mcp_url:
    raise RuntimeError("❌ Falta la variable ENDPOINT_MCP en el .env")

mcp_connections = {
    "InfoAgent": {"transport": "streamable_http", "url": mcp_url},
    "DispoPreciosAgent": {"transport": "streamable_http", "url": mcp_url},
    "InternoAgent": {"transport": "streamable_http", "url": mcp_url},
}

mcp_client = MultiServerMCPClient(mcp_connections)
