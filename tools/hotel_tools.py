# tools/hotel_tools.py
import json
import logging
from langchain.tools import tool as base_tool
from core.mcp_client import mcp_client
from core.utils.normalize_reply import normalize_reply


# =====================================================
# 🧩 Decorador híbrido con compatibilidad universal
# =====================================================
def hybrid_tool(name=None, description=None):
    """
    Decorador híbrido compatible con todas las versiones de LangChain.
    Permite conservar metadatos 'name' y 'description' aunque el decorador
    original @tool no los acepte como argumentos.
    """
    def wrapper(func):
        decorated = base_tool(func)
        decorated.name = name or func.__name__
        decorated.description = description or func.__doc__ or ""
        return decorated
    return wrapper


# =====================================================
# 🧠 Información general del hotel
# =====================================================
@hybrid_tool(
    name="hotel_information",
    description=(
        "Proporciona información general del hotel: servicios, políticas, "
        "ubicación, contacto, instalaciones, normas, horarios o amenities. "
        "Usa esta herramienta cuando el cliente haga preguntas sobre qué "
        "ofrece el hotel, su ubicación o cómo llegar."
    )
)
async def hotel_information_tool(query: str) -> str:
    """Obtiene información general del hotel desde el InfoAgent (MCP)."""
    try:
        tools = await mcp_client.get_tools(server_name="InfoAgent")
        info_tool = next((t for t in tools if t.name == "Base_de_conocimientos_del_hotel"), None)

        if not info_tool:
            return "No dispongo de esa información en este momento."

        raw_reply = await info_tool.ainvoke({"input": query})
        return normalize_reply(raw_reply, query, source="InfoAgent")

    except Exception as e:
        logging.error(f"Error en hotel_information_tool: {e}")
        return "Ocurrió un error consultando la información. Permíteme contactar con el encargado."


# =====================================================
# 💰 Disponibilidad, precios y reservas
# =====================================================
@hybrid_tool(
    name="availability_pricing",
    description=(
        "Consulta disponibilidad, precios y gestiona reservas de habitaciones. "
        "Usa esta herramienta para preguntas sobre precios, disponibilidad, "
        "tarifas, promociones o para realizar una reserva."
    )
)
async def availability_pricing_tool(query: str) -> str:
    """Consulta disponibilidad y precios del hotel (vía DispoPreciosAgent)."""
    try:
        tools = await mcp_client.get_tools(server_name="DispoPreciosAgent")

        # 🔹 Obtener token de autenticación del sistema de reservas
        token = None
        try:
            token_tool = next((t for t in tools if t.name == "buscar_token"), None)
            if not token_tool:
                return "El sistema de reservas no está disponible ahora mismo."
            token_raw = await token_tool.ainvoke({})
            token_data = json.loads(token_raw) if isinstance(token_raw, str) else token_raw
            token = token_data[0].get("key") if isinstance(token_data, list) else token_data.get("key")
        except Exception as e:
            logging.error(f"Error obteniendo token: {e}")
            return "No puedo acceder al sistema de reservas en este momento. Estoy contactando con el encargado."

        if not token:
            return "Sistema de reservas no disponible temporalmente. Contactando con el encargado."

        dispo_tool = next((t for t in tools if t.name == "Disponibilidad_y_precios"), None)
        if not dispo_tool:
            return "No se pudo acceder al módulo de disponibilidad y precios. Contactando con el encargado."

        # 🔹 Fechas de ejemplo (puedes parametrizar dinámicamente después)
        params = {
            "checkin": "2025-10-25T00:00:00",
            "checkout": "2025-10-27T00:00:00",
            "occupancy": 2,
            "key": token,
        }

        raw_reply = await dispo_tool.ainvoke(params)
        rooms = json.loads(raw_reply) if isinstance(raw_reply, str) else raw_reply

        if not rooms:
            return "No hay disponibilidad para esas fechas. ¿Quieres que consulte otras opciones?"

        opciones = "\n".join(
            f"- {r['roomTypeName']}: {r['avail']} disponibles · {r['price']}€/noche"
            for r in rooms
        )
        return f"Disponibilidad del {params['checkin'][:10]} al {params['checkout'][:10]}:\n{opciones}"

    except Exception as e:
        logging.error(f"Error en availability_pricing_tool: {e}")
        return "Error consultando disponibilidad. Voy a contactar con el encargado para ayudarte."


# =====================================================
# 🧍 Escalación a soporte humano
# =====================================================
@hybrid_tool(
    name="guest_support",
    description=(
        "Escalación a soporte humano para casos complejos, errores en otras herramientas, "
        "o consultas que requieren intervención del staff del hotel."
    )
)
async def guest_support_tool(query: str) -> str:
    """Escala la consulta al encargado del hotel (InternoAgent)."""
    try:
        tools = await mcp_client.get_tools(server_name="InternoAgent")
        support_tool = next((t for t in tools if t.name == "Base_de_conocimientos_del_hotel"), None)

        if not support_tool:
            return "Estoy contactando con el encargado del hotel. Te responderemos lo antes posible."

        raw_reply = await support_tool.ainvoke({"input": query})
        return normalize_reply(raw_reply, query, source="InternoAgent")

    except Exception as e:
        logging.error(f"Error en guest_support_tool: {e}")
        return "He contactado con el encargado del hotel y te responderá a la brevedad."


# =====================================================
# 💭 Reflexión / análisis (Think Tool)
# =====================================================
@hybrid_tool(
    name="think_tool",
    description="Reflexiona sobre la situación actual antes de tomar una decisión o elegir una herramienta."
)
def think_tool(situation: str) -> str:
    """Analiza internamente la situación antes de actuar."""
    return f"Analizando la situación: {situation}"


# =====================================================
# 🔁 Exportador general de herramientas
# =====================================================
def get_all_hotel_tools():
    """Retorna todas las herramientas disponibles para el hotel."""
    return [
        hotel_information_tool,
        availability_pricing_tool,
        guest_support_tool,
        think_tool,
    ]
