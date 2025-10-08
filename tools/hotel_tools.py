# =====================================================
# 🏨 HotelAI Tools — herramientas LangChain para el agente híbrido
# =====================================================
import json
import logging
import datetime
import random
import re
from langchain.tools import tool as base_tool
from core.mcp_client import mcp_client
from core.utils.normalize_reply import normalize_reply, summarize_tool_output


# =====================================================
# ⚙️ Decorador híbrido compatible (LangChain <-> HotelAI)
# =====================================================
def hybrid_tool(name=None, description=None, return_direct=False):
    """
    Decorador híbrido compatible con versiones antiguas y nuevas de LangChain.
    Permite conservar metadatos y opcionalmente establecer return_direct=True.
    """
    def wrapper(func):
        decorated = base_tool(func)
        decorated.name = name or func.__name__
        decorated.description = description or func.__doc__ or ""
        # LangChain respeta esta propiedad en agentes con tools
        decorated.return_direct = return_direct
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
    ),
    return_direct=True,  # ✅ Devuelve texto directo al usuario
)
async def hotel_information_tool(query: str) -> str:
    """Obtiene información general del hotel desde el InfoAgent (MCP)."""
    try:
        tools = await mcp_client.get_tools(server_name="InfoAgent")
        info_tool = next(
            (t for t in tools if t.name == "Base_de_conocimientos_del_hotel"), None
        )

        if not info_tool:
            return "No dispongo de esa información en este momento."

        # 🧩 Consultar la base de conocimientos
        raw_reply = await info_tool.ainvoke({"input": query})
        cleaned = normalize_reply(raw_reply, query, source="InfoAgent")

        # 💬 Reformular con tono natural y amable
        final_text = summarize_tool_output(query, cleaned)
        logging.info(f"🔧 hotel_information_tool → {final_text[:160]}...")
        return final_text

    except Exception as e:
        logging.error(f"❌ Error en hotel_information_tool: {e}", exc_info=True)
        return (
            "Ha ocurrido un error al consultar la información del hotel. "
            "Permíteme contactar con el encargado para confirmarlo."
        )


# =====================================================
# 💰 Disponibilidad, precios y reservas
# =====================================================
@hybrid_tool(
    name="availability_pricing",
    description=(
        "Consulta disponibilidad, precios y gestiona reservas de habitaciones. "
        "Usa esta herramienta para preguntas sobre precios, disponibilidad, "
        "tarifas, promociones o para realizar una reserva."
    ),
    return_direct=True,
)
async def availability_pricing_tool(query: str) -> str:
    """Consulta disponibilidad y precios del hotel (vía DispoPreciosAgent)."""
    try:
        logging.info(f"🧩 availability_pricing_tool ejecutado con query: {query}")

        tools = await mcp_client.get_tools(server_name="DispoPreciosAgent")

        # 🔑 Obtener token de autenticación
        token = None
        try:
            token_tool = next((t for t in tools if t.name == "buscar_token"), None)
            if token_tool:
                token_raw = await token_tool.ainvoke({})
                token_data = json.loads(token_raw) if isinstance(token_raw, str) else token_raw
                token = (
                    token_data[0].get("key")
                    if isinstance(token_data, list)
                    else token_data.get("key")
                )
        except Exception as e:
            logging.error(f"Error obteniendo token: {e}")
            return (
                "No puedo acceder al sistema de reservas en este momento. "
                "Estoy contactando con el encargado."
            )

        if not token:
            return "Sistema de reservas no disponible temporalmente. Contactando con el encargado."

        # 🔹 Herramienta de disponibilidad
        dispo_tool = next((t for t in tools if t.name == "Disponibilidad_y_precios"), None)
        if not dispo_tool:
            return "No se pudo acceder al módulo de disponibilidad y precios. Contactando con el encargado."

        # 🔹 Fechas por defecto si no se detectan en el texto
        today = datetime.date.today()
        checkin = today + datetime.timedelta(days=17)
        checkout = checkin + datetime.timedelta(days=2)

        # Detectar ocupantes (si aparece un número en el texto)
        m = re.search(r"\b(\d+)\s*(personas|pax|adultos)?\b", query.lower())
        occupancy = int(m.group(1)) if m else 2

        params = {
            "checkin": f"{checkin}T00:00:00",
            "checkout": f"{checkout}T00:00:00",
            "occupancy": occupancy,
            "key": token,
        }

        # 🔹 Consultar disponibilidad y precios
        raw_reply = await dispo_tool.ainvoke(params)
        rooms = json.loads(raw_reply) if isinstance(raw_reply, str) else raw_reply

        if not rooms:
            return (
                f"No hay disponibilidad del {checkin.strftime('%d/%m/%Y')} "
                f"al {checkout.strftime('%d/%m/%Y')}. ¿Quieres que revise otras fechas?"
            )

        # 🔹 Generar listado de habitaciones
        opciones = "\n".join(
            f"- {r['roomTypeName']}: {r['avail']} disponibles · {r['price']}€/noche"
            for r in rooms
        )

        # 🔹 Detectar tipo solicitado
        ql = query.lower()
        preferida = None
        if "estándar" in ql or "estandar" in ql or "standard" in ql:
            preferida = next((r for r in rooms if "Estándar" in r["roomTypeName"]), None)
        elif "supletoria" in ql:
            preferida = next((r for r in rooms if "Supletoria" in r["roomTypeName"]), None)

        # 🔹 Si el usuario pidió reservar
        if any(x in ql for x in ("reserv", "confirm", "book")):
            seleccion = preferida or random.choice(rooms)
            logging.info("🟢 Reserva directa detectada.")
            return (
                f"✅ Reserva confirmada: habitación {seleccion['roomTypeName'].lower()} "
                f"del {checkin.strftime('%d/%m/%Y')} al {checkout.strftime('%d/%m/%Y')} "
                f"para {occupancy} persona(s), {seleccion['price']}€ por noche. "
                f"¡Gracias por elegirnos! 🏨✨"
            )

        # 🔹 Solo consulta informativa
        respuesta = (
            f"Disponibilidad del {checkin.strftime('%d/%m/%Y')} al {checkout.strftime('%d/%m/%Y')} "
            f"para {occupancy} persona(s):\n"
            f"{opciones}\n\n"
            "Si lo deseas, puedo confirmar la reserva de la opción que prefieras."
        )
        logging.info(f"🔧 availability_pricing_tool → {respuesta[:160]}...")
        return respuesta

    except Exception as e:
        logging.error(f"❌ Error en availability_pricing_tool: {e}", exc_info=True)
        return "Ocurrió un error consultando disponibilidad. Contactaré con el encargado para ayudarte."


# =====================================================
# 🧍 Escalación a soporte humano
# =====================================================
@hybrid_tool(
    name="guest_support",
    description=(
        "Escalación a soporte humano para casos complejos, errores en otras herramientas, "
        "o consultas que requieren intervención del staff del hotel."
    ),
    return_direct=True,
)
async def guest_support_tool(query: str) -> str:
    """Escala la consulta al encargado del hotel (InternoAgent)."""
    try:
        tools = await mcp_client.get_tools(server_name="InternoAgent")
        support_tool = next(
            (t for t in tools if t.name == "Base_de_conocimientos_del_hotel"), None
        )

        if not support_tool:
            return "Estoy contactando con el encargado del hotel. Te responderemos lo antes posible."

        raw_reply = await support_tool.ainvoke({"input": query})
        cleaned = normalize_reply(raw_reply, query, source="InternoAgent")

        final_text = summarize_tool_output(query, cleaned)
        logging.info(f"🔧 guest_support_tool → {final_text[:160]}...")
        return final_text

    except Exception as e:
        logging.error(f"❌ Error en guest_support_tool: {e}", exc_info=True)
        return (
            "He contactado con el encargado del hotel para resolver tu solicitud. "
            "Recibirás respuesta en breve."
        )


# =====================================================
# 💭 Reflexión / análisis interno
# =====================================================
@hybrid_tool(
    name="think_tool",
    description="Reflexiona sobre la situación actual antes de tomar una decisión o elegir una herramienta.",
    return_direct=False,
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
