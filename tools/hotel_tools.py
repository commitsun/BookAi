# =====================================================
# 🏨 HotelAI Tools — herramientas LangChain para el agente híbrido
# =====================================================
import json
import logging
import datetime
import random
import re
import asyncio
from pydantic import BaseModel, Field
from langchain.tools import tool as base_tool, StructuredTool
from core.mcp_client import mcp_client
from core.utils.normalize_reply import normalize_reply, summarize_tool_output


# =====================================================
# ⚙️ Decorador híbrido compatible (LangChain <-> HotelAI)
# =====================================================
def hybrid_tool(name=None, description=None, return_direct=False):
    def wrapper(func):
        if not getattr(func, "__doc__", None):
            func.__doc__ = description or f"Tool: {func.__name__}"
        decorated = base_tool(func)
        decorated.name = name or func.__name__
        decorated.description = description or func.__doc__
        decorated.return_direct = return_direct
        return decorated
    return wrapper


# =====================================================
# 🔧 Constantes y funciones auxiliares
# =====================================================
ESCALATE_SENTENCE = (
    "🕓 Un momento por favor, voy a consultarlo con el encargado. "
    "Permíteme contactar con el encargado."
)


def _looks_external_query(q: str) -> bool:
    """Detecta si el huésped pregunta por cosas FUERA del hotel."""
    ql = (q or "").lower()
    external_kw = [
        "cerca", "alrededor", "próximo", "proximo", "cercanos",
        "near", "around", "close by", "nearby",
        "restaurante", "restaurant", "comida", "chino", "chinese",
        "farmacia", "pharmacy", "parada", "bus stop", "taxi",
        "playa", "beach", "supermercado", "supermarket",
        "museo", "museum", "parking público", "public parking",
    ]
    return any(k in ql for k in external_kw)


def _should_escalate_from_text(text: str) -> bool:
    """Si la respuesta parece error o no dato, devolvemos escalación."""
    if not text:
        return True
    t = text.strip().lower()
    triggers = [
        "no dispongo de ese dato",
        "no dispongo",
        "no hay información",
        "no se encontró",
        "no se pudo",
        "error",
        "respuesta no disponible",
    ]
    return any(p in t for p in triggers)


# =====================================================
# 🧠 Información general del hotel (KB interna, MCP)
# =====================================================
class HotelInformationInput(BaseModel):
    query: str = Field(..., description="Consulta o pregunta del huésped sobre el hotel")

    @classmethod
    def model_validate(cls, data):
        # 🔧 Permitir tanto 'question' como 'query'
        if isinstance(data, dict) and "question" in data and "query" not in data:
            data["query"] = data["question"]
        return super().model_validate(data)


@hybrid_tool(
    name="hotel_information",
    description=(
        "Proporciona información general del hotel: servicios, políticas, "
        "ubicación, contacto, instalaciones, normas, horarios o amenities. "
        "Úsala cuando el cliente pregunte por wifi, desayuno, parking, gimnasio, spa, etc."
    ),
    return_direct=True,
)
async def hotel_information_tool(query: str = None, question: str = None) -> str:
    """Consulta InfoAgent (MCP) → Base_de_conocimientos_del_hotel."""
    try:
        q = (query or question or "").strip()
        if not q:
            return ESCALATE_SENTENCE

        # 🔹 Detecta si el cliente pregunta por algo externo al hotel
        if _looks_external_query(q):
            logging.info("↗️ Consulta externa detectada → escalación automática.")
            return ESCALATE_SENTENCE

        # 🔹 Buscar la tool de conocimiento en el MCP
        tools = await mcp_client.get_tools(server_name="InfoAgent")
        if not tools:
            logging.error("❌ No se encontraron herramientas del InfoAgent (MCP vacío).")
            return ESCALATE_SENTENCE

        info_tool = next((t for t in tools if t.name == "Base_de_conocimientos_del_hotel"), None)
        if not info_tool:
            logging.error("⚠️ No se encontró 'Base_de_conocimientos_del_hotel' en MCP.")
            return ESCALATE_SENTENCE

        # =====================================================
        # 🔎 Llamada directa al MCP
        # =====================================================
        raw_reply = await info_tool.ainvoke({"input": q})

        logging.info(f"📦 RAW REPLY TYPE: {type(raw_reply)}")
        logging.info(f"📦 RAW REPLY CONTENT (primeros 400 chars): {str(raw_reply)[:400]}")

        # =====================================================
        # 🧩 Limpieza y normalización
        # =====================================================
        cleaned = normalize_reply(raw_reply, q, source="InfoAgent").strip()
        if not cleaned or len(cleaned) < 10:
            logging.warning(f"⚠️ KB devolvió vacío o formato raro: {type(raw_reply)}")
            cleaned = str(raw_reply).strip()

        if not cleaned or len(cleaned) < 10:
            return ESCALATE_SENTENCE

        # =====================================================
        # ✨ Reformulación natural con LLM
        # =====================================================
        final_text = summarize_tool_output(q, cleaned)
        if not final_text or len(final_text) < 10:
            final_text = cleaned

        logging.info(f"✅ Respuesta final hotel_information_tool → {final_text[:200]}...")
        return final_text

    except Exception as e:
        logging.error(f"💥 Error en hotel_information_tool: {e}", exc_info=True)
        return ESCALATE_SENTENCE


# =====================================================
# 💰 Disponibilidad, precios y reservas
# =====================================================
@hybrid_tool(
    name="availability_pricing",
    description=(
        "Consulta disponibilidad, precios y gestiona reservas de habitaciones. "
        "Úsala cuando el cliente pregunte por precio, fechas, ofertas, número de camas o reserva."
    ),
    return_direct=True,
)
async def availability_pricing_tool(query: str) -> str:
    """Consulta DispoPreciosAgent (MCP): buscar_token + Disponibilidad_y_precios."""
    try:
        logging.info(f"🧩 availability_pricing_tool ejecutado con query: {query}")

        tools = await mcp_client.get_tools(server_name="DispoPreciosAgent")
        if not tools:
            logging.error("❌ No se encontraron herramientas del DispoPreciosAgent.")
            return ESCALATE_SENTENCE

        token_tool = next((t for t in tools if t.name == "buscar_token"), None)
        dispo_tool = next((t for t in tools if t.name == "Disponibilidad_y_precios"), None)
        if not token_tool or not dispo_tool:
            logging.error("⚠️ Faltan tools requeridas en MCP.")
            return ESCALATE_SENTENCE

        token_raw = await token_tool.ainvoke({})
        token_data = json.loads(token_raw) if isinstance(token_raw, str) else token_raw
        token = (
            token_data[0].get("key") if isinstance(token_data, list)
            else token_data.get("key")
        )
        if not token:
            logging.error("⚠️ No se obtuvo token válido de buscar_token.")
            return ESCALATE_SENTENCE

        today = datetime.date.today()
        checkin = today + datetime.timedelta(days=17)
        checkout = checkin + datetime.timedelta(days=2)

        m = re.search(r"\b(\d+)\s*(personas|pax|adultos)?\b", (query or "").lower())
        occupancy = int(m.group(1)) if m else 2

        params = {
            "checkin": f"{checkin}T00:00:00",
            "checkout": f"{checkout}T00:00:00",
            "occupancy": occupancy,
            "key": token,
        }

        raw_reply = await dispo_tool.ainvoke(params)
        rooms = json.loads(raw_reply) if isinstance(raw_reply, str) else raw_reply
        if not rooms:
            logging.warning("⚠️ Disponibilidad vacía desde MCP → escalación.")
            return ESCALATE_SENTENCE

        opciones = "\n".join(
            f"- {r['roomTypeName']}: {r['avail']} disponibles · {r['price']}€/noche"
            for r in rooms
        )

        ql = (query or "").lower()
        preferida = None
        if "estándar" in ql or "estandar" in ql or "standard" in ql:
            preferida = next(
                (r for r in rooms if any(w in r["roomTypeName"].lower() for w in ["estándar", "estandar", "standard"])),
                None,
            )
        elif "supletoria" in ql:
            preferida = next((r for r in rooms if "supletoria" in r["roomTypeName"].lower()), None)

        if any(x in ql for x in ("reserv", "confirm", "book")) and (preferida or rooms):
            seleccion = preferida or random.choice(rooms)
            return (
                f"✅ Reserva confirmada: habitación {seleccion['roomTypeName'].lower()} "
                f"del {checkin.strftime('%d/%m/%Y')} al {checkout.strftime('%d/%m/%Y')} "
                f"para {occupancy} persona(s), {seleccion['price']}€ por noche. "
                f"¡Gracias por elegirnos! 🏨✨"
            )

        respuesta = (
            f"Disponibilidad del {checkin.strftime('%d/%m/%Y')} al {checkout.strftime('%d/%m/%Y')} "
            f"para {occupancy} persona(s):\n{opciones}\n\n"
            "Si quieres, puedo confirmar la reserva de la opción que prefieras."
        )
        return respuesta

    except Exception as e:
        logging.error(f"❌ Error en availability_pricing_tool: {e}", exc_info=True)
        return ESCALATE_SENTENCE


# =====================================================
# 🧍 Escalación a soporte humano
# =====================================================
@hybrid_tool(
    name="guest_support",
    description=(
        "Escala la consulta al encargado humano del hotel. "
        "Úsala cuando la información no está disponible o el cliente pida hablar con alguien."
    ),
    return_direct=True,
)
async def guest_support_tool(query: str) -> str:
    """Devuelve mensaje de escalación. La gestión humana ocurre fuera (InternoAgent)."""
    return ESCALATE_SENTENCE


# =====================================================
# 💭 Reflexión interna
# =====================================================
@hybrid_tool(
    name="think_tool",
    description="Reflexión interna antes de elegir otra tool (no se envía al huésped).",
    return_direct=False,
)
def think_tool(situation: str) -> str:
    return f"Analizando la situación: {situation}"


# =====================================================
# 👋 Conversación trivial / saludo
# =====================================================
@hybrid_tool(
    name="other",
    description="Para saludos, agradecimientos o small talk. Devuelve el texto directamente.",
    return_direct=True,
)
def other_tool(reply: str) -> str:
    return (reply or "").strip()


# =====================================================
# 🧩 Adaptador para tools async (seguro para FastAPI + LangChain)
# =====================================================
def make_async_tool_sync(tool_func, name, description):
    from langchain_core.tools import BaseTool

    async def async_wrapper(tool_input=None, **kwargs):
        if isinstance(tool_func, BaseTool):
            if tool_input is not None:
                return await tool_func.ainvoke(tool_input)
            return await tool_func.ainvoke(kwargs)
        if tool_input is not None:
            if isinstance(tool_input, dict):
                return await tool_func(**tool_input)
            elif isinstance(tool_input, str):
                try:
                    return await tool_func(query=tool_input)
                except TypeError:
                    return await tool_func(tool_input)
        return await tool_func(**kwargs)

    def sync_wrapper(tool_input=None, **kwargs):
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                raise RuntimeError("Loop cerrado")
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(async_wrapper(tool_input=tool_input, **kwargs))
        finally:
            if not loop.is_running():
                loop.close()

    return StructuredTool.from_function(
        sync_wrapper, name=name, description=description, return_direct=True
    )


# =====================================================
# 🔁 Exportador general de herramientas
# =====================================================
def get_all_hotel_tools():
    return [
        make_async_tool_sync(hotel_information_tool, "hotel_information", hotel_information_tool.description),
        make_async_tool_sync(availability_pricing_tool, "availability_pricing", availability_pricing_tool.description),
        make_async_tool_sync(guest_support_tool, "guest_support", guest_support_tool.description),
        think_tool,
        other_tool,
    ]
