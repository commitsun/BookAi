import asyncio
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
    "InfoAgent": {"transport": "streamable_http", "url": mcp_url},
    "DispoPreciosAgent": {"transport": "streamable_http", "url": mcp_url},
    "OnboardingAgent": {"transport": "streamable_http", "url": mcp_url},
    # InternoAgent NO usa MCP — es local (Telegram + Supabase)
}

def _build_client() -> MultiServerMCPClient:
    return MultiServerMCPClient(mcp_connections)


# Inicializar el cliente multi-servidor
mcp_client = _build_client()

# =====================================================
# 🧩 FUNCIONES AUXILIARES
# =====================================================
async def get_tools(server_name: str, retries: int = 1, retry_delay: float = 0.4):
    """
    Wrapper resiliente para obtener tools desde MCP.
    Reintenta y recrea el cliente si la sesión se cierra inesperadamente.
    """
    global mcp_client
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return await mcp_client.get_tools(server_name=server_name)
        except Exception as exc:
            last_exc = exc
            msg = str(exc)
            if "Session terminated" in msg or "session terminated" in msg:
                logger.warning("⚠️ MCP sesión terminada (%s). Reiniciando cliente (intento %s/%s).", server_name, attempt + 1, retries + 1)
                mcp_client = _build_client()
                await asyncio.sleep(retry_delay)
                continue
            logger.error("❌ Error obteniendo tools de %s: %s", server_name, exc, exc_info=True)
            break
    if last_exc:
        logger.error("❌ Fallo definitivo obteniendo tools de %s: %s", server_name, last_exc, exc_info=True)
    return []


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
        tools = await get_tools(server_name=server_name)
        if not tools:
            logger.warning(f"⚠️ No se encontraron tools para {server_name}")
            return []

        filtered = []
        for t in tools:
            n = t.name.lower()

            if server_name == "InfoAgent" and any(k in n for k in ["base", "conocimiento", "knowledge", "google", "web","search"]):
                filtered.append(t)

            elif server_name == "DispoPreciosAgent" and any(k in n for k in ["disponibilidad", "precio", "token"]):
                filtered.append(t)

            elif server_name == "OnboardingAgent" and any(
                k in n for k in ["token", "habitacion", "reserva", "reserv", "booking", "crear"]
            ):
                if "multireserva" in n:
                    continue
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
