# agents/base_agent.py
from fastmcp import FastMCP
from core.config import Settings as C
from langchain_mcp_adapters.client import MCPClient

class MCPBackedAgent:
    """Agente base MCP — usado por todos los agentes inteligentes."""
    # Inicializa el estado interno y las dependencias de `MCPBackedAgent`.
    # Se usa dentro de `MCPBackedAgent` en el flujo de clases base de agentes.
    # Recibe `name` como entrada principal según la firma.
    # No devuelve valor; deja la instancia preparada con sus dependencias y estado inicial. Sin efectos secundarios relevantes.
    def __init__(self, name: str):
        self.mcp = FastMCP(name)
        self.kb_client = MCPClient(transport="streamable_http", url=C.ENDPOINT_MCP)
