import os
import logging
import aiohttp
import asyncio

# =====================================================
# 🔧 CONFIGURACIÓN BÁSICA
# =====================================================
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

MCP_LOCAL = "http://bookai_mcp_server:8001"

# =====================================================
# 🌐 FUNCIONES HTTP DIRECTAS
# =====================================================

async def call_knowledge_base(query: str, match_count: int = 7):
    """
    Llama al MCP Server para consultar la base de conocimientos.
    Endpoint: POST /tools/knowledge_base
    """
    url = f"{MCP_LOCAL}/tools/knowledge_base"
    payload = {"query": query, "match_count": match_count}
    timeout = aiohttp.ClientTimeout(total=30)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"❌ Error HTTP {resp.status}: {text}")
                    return {"error": text}
                return await resp.json()

    except asyncio.TimeoutError:
        logger.warning("⚠️ Timeout al consultar knowledge_base (30s). Se usará fallback local.")
        return {"error": "timeout"}

    except aiohttp.ClientError as e:
        logger.error(f"🌐 Error de red en knowledge_base: {e}")
        return {"error": str(e)}

    except Exception as e:
        logger.error(f"❌ Error inesperado en call_knowledge_base: {e}", exc_info=True)
        return {"error": str(e)}


async def call_availability_pricing(checkin: str, checkout: str, occupancy: int, pms_property_id: int = 38):
    """
    Llama al endpoint de disponibilidad y precios del MCP Server.
    Endpoint: POST /tools/availability_pricing
    """
    url = f"{MCP_LOCAL}/tools/availability_pricing"
    payload = {
        "checkin": checkin,
        "checkout": checkout,
        "occupancy": occupancy,
        "pms_property_id": pms_property_id,
    }
    timeout = aiohttp.ClientTimeout(total=30)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"❌ Error HTTP {resp.status}: {text}")
                    return {"error": text}
                return await resp.json()

    except asyncio.TimeoutError:
        logger.warning("⚠️ Timeout al consultar availability_pricing (30s).")
        return {"error": "timeout"}

    except aiohttp.ClientError as e:
        logger.error(f"🌐 Error de red en availability_pricing: {e}")
        return {"error": str(e)}

    except Exception as e:
        logger.error(f"❌ Error inesperado en call_availability_pricing: {e}", exc_info=True)
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
