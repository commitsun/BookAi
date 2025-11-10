import os
import logging
import aiohttp
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

# =====================================================
# 🔗 CONEXIONES ACTIVAS
# =====================================================
# Solo DispoPreciosAgent usa el protocolo MCP nativo (streamable_http)
mcp_connections = {
    "DispoPreciosAgent": {"transport": "streamable_http", "url": mcp_url},
}

# Inicializar cliente MCP solo para los agentes que realmente lo usan
mcp_client = MultiServerMCPClient(mcp_connections)

# =====================================================
# 🌐 FUNCIONES HTTP (para InfoAgent)
# =====================================================
async def call_knowledge_base(query: str, match_count: int = 7):
    """
    Llama directamente al MCP Server HTTP (FastAPI) para consultar la base de conocimientos.
    Endpoint: POST /tools/knowledge_base
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{mcp_local}/tools/knowledge_base",
                json={"query": query, "match_count": match_count},
                timeout=30,
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"❌ Error HTTP {resp.status}: {text}")
                    return {"error": text}
                return await resp.json()
    except Exception as e:
        logger.error(f"❌ Error al llamar knowledge_base: {e}", exc_info=True)
        return {"error": str(e)}

# =====================================================
# 🧩 FUNCIONES AUXILIARES (para DispoPreciosAgent)
# =====================================================
async def get_filtered_tools(server_name: str):
    """
    Devuelve las tools relevantes para un servidor MCP concreto.
    - InfoAgent → no usa MCP, se maneja por HTTP.
    - DispoPreciosAgent → usa disponibilidad, precios y buscar_token.
    """
    if server_name == "InfoAgent":
        logger.info("ℹ️ InfoAgent usa API HTTP directa, no MCP.")
        return []

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
            if server_name == "DispoPreciosAgent" and any(
                k in n for k in ["disponibilidad", "precio", "token"]
            ):
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
