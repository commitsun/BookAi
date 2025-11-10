import os
import logging
from langchain_mcp_adapters.client import MultiServerMCPClient

# =====================================================
# 🔧 CONFIGURACIÓN BÁSICA
# =====================================================
logging.getLogger("mcp").setLevel(logging.ERROR)
logger = logging.getLogger(__name__)

# Leer endpoint desde .env
mcp_url = os.getenv("ENDPOINT_MCP")
mcp_local = "http://bookai_mcp_server:8001"
if not mcp_url:
    raise RuntimeError("❌ Falta la variable ENDPOINT_MCP en el .env")

# 🔗 Conexiones MCP activas
# Solo los agentes que realmente usan workflows remotos
mcp_connections = {
    "InfoAgent": {"transport": "streamable_http", "url": mcp_local},
    "DispoPreciosAgent": {"transport": "streamable_http", "url": mcp_url},
    # InternoAgent NO usa MCP — es local (Telegram + Supabase)
}

# Inicializar el cliente multi-servidor
mcp_client = MultiServerMCPClient(mcp_connections)

# =====================================================
# 🧩 FUNCIONES AUXILIARES
# =====================================================
async def get_filtered_tools(server_name: str):
    """
    Devuelve las tools relevantes para un servidor MCP concreto.
    - InfoAgent → base de conocimientos y buscar_token
    - DispoPreciosAgent → disponibilidad, precios y buscar_token
    - InternoAgent → no aplica (usa canal local)
    """
    if server_name == "InternoAgent":
        logger.info("ℹ️ InternoAgent no requiere MCP (usa canal local).")
        return []

    try:
        tools = await mcp_client.get_tools(server_name=server_name)
        if not tools:
            logger.warning(f"⚠️ No se encontraron tools para {server_name}")
            return []

        filtered = []
        for t in tools:
            n = t.name.lower()

            if server_name == "InfoAgent" and any(k in n for k in ["base", "conocimiento", "knowledge", "token"]):
                filtered.append(t)

            elif server_name == "DispoPreciosAgent" and any(k in n for k in ["disponibilidad", "precio", "token"]):
                filtered.append(t)

        logger.info(f"[MCP] Tools para {server_name}: {[t.name for t in filtered]}")
        return filtered

    except Exception as e:
        logger.error(f"❌ Error obteniendo tools de {server_name}: {e}", exc_info=True)
        return []


# =====================================================
# 🎧 EVENT LISTENER
# =====================================================
async def listen_events(client: MultiServerMCPClient, handle_event):
    """
    Escucha eventos del MCP Server y los reenvía al manejador proporcionado.
    Solo procesa eventos relevantes (message, tool, tool_result).
    """
    async for event in client.events():
        if event.type not in ["message", "tool", "tool_result"]:
            continue
        handle_event(event)
