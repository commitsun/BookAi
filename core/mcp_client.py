import os
import logging
from langchain_mcp_adapters.client import MultiServerMCPClient

# Configuración de logging: ocultar eventos SSE no relevantes
logging.getLogger("mcp").setLevel(logging.ERROR)

# Cargar URL desde variables de entorno
mcp_url = os.getenv("ENDPOINT_MCP")
if not mcp_url:
    raise RuntimeError("❌ Falta la variable ENDPOINT_MCP en el .env")

# Definición de conexiones disponibles
mcp_connections = {
    "InfoAgent": {"transport": "streamable_http", "url": mcp_url},
    "DispoPreciosAgent": {"transport": "streamable_http", "url": mcp_url},
    "InternoAgent": {"transport": "streamable_http", "url": mcp_url},
}

# Inicializar cliente multi-servidor
mcp_client = MultiServerMCPClient(mcp_connections)

async def listen_events(client: MultiServerMCPClient, handle_event):
    async for event in client.events():
        # Ignorar "endpoint" u otros eventos desconocidos
        if event.type == "endpoint":
            continue

        if event.type in ["message", "tool", "tool_result"]:
            handle_event(event)
        # else:
        #     print(f"Evento ignorado: {event.type}")
