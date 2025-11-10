import os
import logging
import aiohttp

# =====================================================
# 🔧 CONFIGURACIÓN BÁSICA
# =====================================================
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# MCP Server interno (contenedor o localhost)
MCP_LOCAL = "http://bookai_mcp_server:8001"

# =====================================================
# 🌐 FUNCIONES HTTP DIRECTAS
# =====================================================

async def call_knowledge_base(query: str, match_count: int = 7):
    """
    Llama directamente al MCP Server HTTP (FastAPI) para consultar la base de conocimientos.
    Endpoint: POST /tools/knowledge_base
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{MCP_LOCAL}/tools/knowledge_base",
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

async def call_availability_pricing(checkin: str, checkout: str, occupancy: int, pms_property_id: int = 38):
    """
    Llama al endpoint de disponibilidad y precios del MCP Server.
    Endpoint: POST /tools/availability_pricing
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{MCP_LOCAL}/tools/availability_pricing",
                json={
                    "checkin": checkin,
                    "checkout": checkout,
                    "occupancy": occupancy,
                    "pms_property_id": pms_property_id
                },
                timeout=30,
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"❌ Error HTTP {resp.status}: {text}")
                    return {"error": text}
                return await resp.json()
    except Exception as e:
        logger.error(f"❌ Error al llamar availability_pricing: {e}", exc_info=True)
        return {"error": str(e)}

# =====================================================
# 🧩 FUNCIONES AUXILIARES
# =====================================================

async def get_filtered_tools(server_name: str):
    """
    Mock para mantener compatibilidad: devuelve tools relevantes.
    """
    if server_name == "InfoAgent":
        return ["knowledge_base"]
    elif server_name == "DispoPreciosAgent":
        return ["availability_pricing"]
    return []

