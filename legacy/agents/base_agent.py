# agents/base_agent.py
from fastmcp import FastMCP
from core.config import Settings as C
from langchain_mcp_adapters.client import MCPClient

# Agente base MCP — usado por todos los agentes inteligentes.
# Se usa en el flujo de clases base de agentes como pieza de organización, contrato de datos o punto de extensión.
# Se instancia con configuración, managers, clients o callbacks externos y luego delega el trabajo en sus métodos.
# Los efectos reales ocurren cuando sus métodos se invocan; la definición de clase solo organiza estado y responsabilidades.
class MCPBackedAgent:
    """Agente base MCP — usado por todos los agentes inteligentes."""
    # Inicializa el estado interno y las dependencias de `MCPBackedAgent`.
    # Se usa dentro de `MCPBackedAgent` en el flujo de clases base de agentes.
    # Recibe `name` como entrada principal según la firma.
    # No devuelve valor; deja la instancia preparada con sus dependencias y estado inicial. Sin efectos secundarios relevantes.
    def __init__(self, name: str):
        self.mcp = FastMCP(name)
        self.kb_client = MCPClient(transport="streamable_http", url=C.ENDPOINT_MCP)
