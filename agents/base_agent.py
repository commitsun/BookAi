# agents/base_agent.py
from fastmcp import FastMCP
from core.config import Settings as C
from langchain_mcp_adapters.client import MCPClient

class MCPBackedAgent:
    """Agente base MCP — usado por todos los agentes inteligentes."""
    def __init__(self, name: str):
        self.mcp = FastMCP(name)
        self.kb_client = MCPClient(transport="streamable_http", url=C.ENDPOINT_MCP)
