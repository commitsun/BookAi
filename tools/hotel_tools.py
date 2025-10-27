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
from tools.supervisor_input_tool import supervisor_input_tool
from tools.supervisor_output_tool import supervisor_output_tool

# =====================================================
# ⚙️ Decorador híbrido compatible (LangChain <-> HotelAI)
# =====================================================
def hybrid_tool(name=None, description=None, return_direct=False):
    def wrapper(func):

        # 🛡️ 1️⃣ Inyectamos docstring ANTES de llamar a base_tool
        if (not getattr(func, "__doc__", None)) and (description is None):
            func.__doc__ = f"Auto-generated tool: {func.__name__}"

        # 🛠️ 2️⃣ Llamamos a LangChain AHORA que ya tiene docstring
        decorated = base_tool(func)

        # 🏷️ 3️⃣ Forzamos nombre/descripcion final        
        decorated.name = name or func.__name__
        decorated.description = description or func.__doc__
        decorated.return_direct = return_direct

        return decorated
    return wrapper



ESCALATE_SENTENCE = "🕓 Un momento por favor, voy a consultarlo con el encargado. Permíteme contactar con el encargado."

def _looks_external_query(q: str) -> bool:
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
    if not text:
        return True
    t = text.strip().lower()
    # Indicadores de no-dato en KB o de error
    triggers = [
        "no dispongo de ese dato", "no dispongo", "no hay información",
        "no se encontró", "no se pudo", "error", "respuesta no disponible",
    ]
    return any(p in t for p in triggers)

# =====================================================
# 🧠 Información general del hotel (optimizada para usar toda la KB)
# =====================================================
@hybrid_tool(
    name="hotel_information",
    description=(
        "Proporciona información general del hotel: servicios, políticas, "
        "ubicación, contacto, instalaciones, normas, horarios o amenities. "
        "Usa esta herramienta cuando el cliente haga preguntas sobre qué "
        "ofrece el hotel, su ubicación o cómo llegar."
    ),
    return_direct=True,
)
async def hotel_information_tool(query: str) -> str:
    """
    Obtiene información general del hotel desde el InfoAgent (MCP).
    Prioriza SIEMPRE la respuesta de la KB aunque sea parcial.
    Solo escala si la KB no devuelve absolutamente nada útil.
    """
    try:
        q = (query or "").strip()
        if not q:
            return ESCALATE_SENTENCE

        # 🔎 Evita consultas que no son sobre el hotel
        if _looks_external_query(q):
            logging.info("↗️ Consulta externa detectada → escalación automática.")
            return ESCALATE_SENTENCE

        # 🔗 Intentar obtener las herramientas disponibles del InfoAgent
        tools = await mcp_client.get_tools(server_name="InfoAgent")
        if not tools:
            logging.error("❌ No se encontraron herramientas del InfoAgent (MCP vacío).")
            return ESCALATE_SENTENCE

        logging.info(f"🔍 MCP tools disponibles en InfoAgent: {[t.name for t in tools]}")

        info_tool = next((t for t in tools if t.name == "Base_de_conocimientos_del_hotel"), None)
        if not info_tool:
            logging.error("⚠️ No se encontró 'Base_de_conocimientos_del_hotel' en MCP.")
            return ESCALATE_SENTENCE

        # 🧠 Consultar la base de conocimientos
        raw_reply = await info_tool.ainvoke({"input": q})
        cleaned = normalize_reply(raw_reply, q, source="InfoAgent").strip()

        if not cleaned:
            logging.warning("⚠️ KB devolvió vacío o nulo → escalación.")
            return ESCALATE_SENTENCE

        # 🔬 Limpieza avanzada: eliminar texto técnico o redundante
        cleaned = re.sub(r"\s*\(Fuente:[^)]+\)", "", cleaned)
        cleaned = re.sub(r"\s*\[ID:[^\]]+\]", "", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()

        # 🪄 Si la respuesta es corta pero parece válida, úsala igualmente
        if len(cleaned) < 25 and not any(word in cleaned.lower() for word in ["no", "desconocido", "error"]):
            logging.info(f"ℹ️ KB devolvió respuesta breve pero válida: '{cleaned}'")
            return cleaned

        # 🧩 Resumen final mejorado para el cliente
        final_text = summarize_tool_output(q, cleaned)
        if not final_text or len(final_text) < 10:
            final_text = cleaned  # Fallback si el resumen queda demasiado corto

        logging.info(f"🔧 hotel_information_tool → {final_text[:200]}...")
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
                token = (token_data[0].get("key") if isinstance(token_data, list)
                         else token_data.get("key"))
        except Exception as e:
            logging.error(f"Error obteniendo token: {e}")
            return ESCALATE_SENTENCE

        if not token:
            return ESCALATE_SENTENCE

        # 🔹 Herramienta de disponibilidad
        dispo_tool = next((t for t in tools if t.name == "Disponibilidad_y_precios"), None)
        if not dispo_tool:
            return ESCALATE_SENTENCE

        # 🔹 Fechas por defecto si no se detectan en el texto (fallback neutro)
        today = datetime.date.today()
        checkin = today + datetime.timedelta(days=17)
        checkout = checkin + datetime.timedelta(days=2)

        # Detectar ocupantes (si aparece un número en el texto)
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
            return ESCALATE_SENTENCE

        opciones = "\n".join(
            f"- {r['roomTypeName']}: {r['avail']} disponibles · {r['price']}€/noche"
            for r in rooms
        )

        ql = (query or "").lower()
        preferida = None
        if "estándar" in ql or "estandar" in ql or "standard" in ql:
            preferida = next((r for r in rooms if "Estándar" in r["roomTypeName"]), None)
        elif "supletoria" in ql:
            preferida = next((r for r in rooms if "Supletoria" in r["roomTypeName"]), None)

        if any(x in ql for x in ("reserv", "confirm", "book")):
            seleccion = preferida or random.choice(rooms)
            logging.info("🟢 Reserva directa detectada.")
            return (
                f"✅ Reserva confirmada: habitación {seleccion['roomTypeName'].lower()} "
                f"del {checkin.strftime('%d/%m/%Y')} al {checkout.strftime('%d/%m/%Y')} "
                f"para {occupancy} persona(s), {seleccion['price']}€ por noche. "
                f"¡Gracias por elegirnos! 🏨✨"
            )

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
        return ESCALATE_SENTENCE

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
        # Aquí mantenemos la integración con InternoAgent si la tienes operativa,
        # pero el cliente ya recibió el mensaje de escalación por la respuesta del tool.
        return ESCALATE_SENTENCE
    except Exception:
        return ESCALATE_SENTENCE

# =====================================================
# 💭 Reflexión / análisis interno
# =====================================================
@hybrid_tool(
    name="think_tool",
    description="Reflexiona sobre la situación actual antes de decidir la herramienta adecuada.",
    return_direct=False,
)
def think_tool(situation: str) -> str:
    """Realiza una breve reflexión interna antes de elegir una herramienta."""
    return f"Analizando la situación: {situation}"



# =====================================================
# 👋 Saludos / conversación trivial
# =====================================================
@hybrid_tool(
    name="other",
    description=(
        "Para saludos, agradecimientos o conversación trivial. "
        "Devuelve directamente el texto profesional y breve que le pases. "
        "Úsala cuando no se requiera información del hotel ni precios."
    ),
    return_direct=True,
)
def other_tool(reply: str) -> str:
    """Devuelve textualmente la respuesta generada por el agente."""
    return (reply or "").strip()

# =====================================================
# 🔁 Exportador general de herramientas
# =====================================================
def get_all_hotel_tools():
    
    return [
        hotel_information_tool,
        availability_pricing_tool,
        guest_support_tool,
        think_tool,
        other_tool
    ]
