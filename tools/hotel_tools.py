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
from core.utils.normalize_reply import normalize_reply
from langchain.tools import Tool
from langchain_openai import ChatOpenAI

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
# 🧠 Función de resumen de la salida MCP
# =====================================================
async def summarize_tool_output(question: str, context: str) -> str:
    """Resume la información del MCP en una respuesta natural al huésped."""
    try:
        llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.3)
        prompt = f"""
        Eres un asistente del hotel. Un huésped ha hecho la siguiente pregunta: "{question}".

        A continuación tienes información del hotel extraída de una base de datos interna.
        Usa **únicamente la información directamente relacionada con la pregunta**.
        No incluyas detalles de otros temas ni repitas respuestas anteriores.
        Si la información no está explícitamente en el texto, indica amablemente que no dispones de ese dato.

        Devuelve una respuesta breve, amable y clara en español.

        --- Información del hotel ---
        {context}
        """
        response = await llm.ainvoke(prompt)
        return response.content.strip()
    except Exception as e:
        logging.error(f"⚠️ Error al resumir salida del MCP: {e}")
        return context[:500]

# =====================================================
# 🧠 Información general del hotel (KB interna, MCP)
# =====================================================
class HotelInformationInput(BaseModel):
    query: str = Field(..., description="Consulta o pregunta del huésped sobre el hotel")

    @classmethod
    def model_validate(cls, data):
        if isinstance(data, dict) and "question" in data and "query" not in data:
            data["query"] = data["question"]
        return super().model_validate(data)

@hybrid_tool(
    name="hotel_information",
    description=(
        "Proporciona información general del hotel: servicios, políticas, "
        "ubicación, contacto, instalaciones, normas, horarios o amenities. "
        "Úsala cuando el cliente pregunte por wifi, desayuno, parking, gimnasio, spa, atracciones cercanas o actividades turísticas."
    ),
    return_direct=True,
)
async def hotel_information_tool(query: str = None, question: str = None) -> str:
    """Consulta InfoAgent (MCP) → Base_de_conocimientos_del_hotel."""
    try:
        q = (query or question or "").strip()
        if not q:
            return ESCALATE_SENTENCE

        tools = await mcp_client.get_tools(server_name="InfoAgent")
        if not tools:
            logging.error("❌ No se encontraron herramientas del InfoAgent (MCP vacío).")
            return ESCALATE_SENTENCE

        info_tool = next((t for t in tools if t.name == "Base_de_conocimientos_del_hotel"), None)
        if not info_tool:
            logging.error("⚠️ No se encontró 'Base_de_conocimientos_del_hotel' en MCP.")
            return ESCALATE_SENTENCE

        raw_reply = await info_tool.ainvoke({"input": q})
        logging.info(f"📦 RAW REPLY CONTENT (primeros 400 chars): {str(raw_reply)[:400]}")

        cleaned = normalize_reply(raw_reply, q).strip()
        if not cleaned or len(cleaned) < 10:
            cleaned = str(raw_reply).strip()

        if not cleaned or len(cleaned) < 10:
            return ESCALATE_SENTENCE

        summarized = await summarize_tool_output(q, cleaned)
        if not summarized or len(summarized) < 10:
            summarized = cleaned

        logging.info(f"✅ Resumen final hotel_information_tool → {summarized[:200]}...")
        return summarized

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
        "Permite responder preguntas específicas como precios, tipos de habitación, "
        "coste total o disponibilidad para determinadas fechas."
    ),
    return_direct=True,
)
async def availability_pricing_tool(query: str) -> str:
    """Consulta DispoPreciosAgent (MCP): buscar_token + Disponibilidad_y_precios con razonamiento dinámico."""
    from langchain_openai import ChatOpenAI

    try:
        logging.info(f"🧩 availability_pricing_tool ejecutado con query: {query}")

        # 1️⃣ Cargar herramientas
        tools = await mcp_client.get_tools(server_name="DispoPreciosAgent")
        if not tools:
            return ESCALATE_SENTENCE

        token_tool = next((t for t in tools if t.name == "buscar_token"), None)
        dispo_tool = next((t for t in tools if t.name == "Disponibilidad_y_precios"), None)
        if not token_tool or not dispo_tool:
            return ESCALATE_SENTENCE

        # 2️⃣ Obtener token
        token_raw = await token_tool.ainvoke({})
        token_data = json.loads(token_raw) if isinstance(token_raw, str) else token_raw
        token = (
            token_data[0].get("key") if isinstance(token_data, list)
            else token_data.get("key")
        )
        if not token:
            return "⚠️ No se pudo obtener el token de acceso."

        # 3️⃣ Fechas dinámicas
        today = datetime.date.today()
        checkin = today + datetime.timedelta(days=7)
        checkout = checkin + datetime.timedelta(days=2)

        params = {
            "checkin": f"{checkin}T00:00:00",
            "checkout": f"{checkout}T00:00:00",
            "occupancy": 2,
            "key": token,
        }

        # 4️⃣ Llamar al MCP
        raw_reply = await dispo_tool.ainvoke(params)
        rooms = json.loads(raw_reply) if isinstance(raw_reply, str) else raw_reply
        if not rooms or not isinstance(rooms, list):
            return "No hay disponibilidad en las fechas indicadas."

        # 5️⃣ Enviar al LLM para razonamiento libre
        llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.2)
        prompt = f"""
Eres el asistente de reservas del hotel. Dispones de la siguiente información de habitaciones y precios (en euros por noche):

{json.dumps(rooms, ensure_ascii=False, indent=2)}

El huésped pregunta: "{query}"

Tu tarea:
- Usa exclusivamente los datos de la lista para calcular o responder.
- Si se pregunta por el total de noches, multiplica el precio por el número de noches (usa 2 noches por defecto si no se menciona otra duración).
- Si el tipo de habitación no existe en la lista, indícalo amablemente.
- Responde de manera natural y clara, en español, sin mostrar JSON ni datos técnicos.
"""
        response = await llm.ainvoke(prompt)
        answer = response.content.strip()

        logging.info(f"🧠 Respuesta LLM pricing → {answer}")
        return answer

    except Exception as e:
        logging.error(f"❌ Error en availability_pricing_tool flexible: {e}", exc_info=True)
        return ESCALATE_SENTENCE


# =====================================================
# 🧍 Escalación a soporte humano
# =====================================================
@hybrid_tool(
    name="guest_support",
    description="Escala la consulta al encargado humano del hotel.",
    return_direct=True,
)
async def guest_support_tool(query: str) -> str:
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
# 🔁 Exportador general de herramientas (MCP + locales)
# =====================================================
async def load_mcp_tools():
    """Carga herramientas de InfoAgent y DispoPreciosAgent desde el MCP."""
    all_mcp_tools = []
    for server in ["InfoAgent", "DispoPreciosAgent"]:
        try:
            tools = await mcp_client.get_tools(server_name=server)
            all_mcp_tools.extend(tools)
            logging.info(f"✅ {len(tools)} herramientas cargadas desde {server}")
        except Exception as e:
            logging.warning(f"⚠️ No se pudieron cargar herramientas desde {server}: {e}")
    return all_mcp_tools


def get_all_hotel_tools():
    """Obtiene todas las herramientas, incluyendo las del MCP, sin conflictos de asyncio ni pydantic."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            logging.info("🔄 Loop activo detectado → saltando carga directa de MCP (async)")
            mcp_tools = []
        else:
            mcp_tools = loop.run_until_complete(load_mcp_tools())
    except RuntimeError:
        mcp_tools = asyncio.run(load_mcp_tools())

    def wrap_async_tool(fn, name, desc):
        """Convierte async functions o StructuredTools en sync Tools compatibles con LangChain."""
        import asyncio
        import nest_asyncio
        from langchain_core.tools import BaseTool

        def sync_fn(input_text: str):
            try:
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)

                if loop.is_running():
                    nest_asyncio.apply()

                async def run_async():
                    if isinstance(fn, BaseTool):
                        return await fn.ainvoke(input_text)
                    elif asyncio.iscoroutinefunction(fn):
                        return await fn(input_text)
                    else:
                        return fn(input_text)

                return loop.run_until_complete(run_async())

            except Exception as e:
                logging.error(f"Error en {name}: {e}", exc_info=True)
                return ESCALATE_SENTENCE

        return Tool(
            name=name,
            func=sync_fn,
            description=desc,
            return_direct=True,
        )

    tools = [
        wrap_async_tool(hotel_information_tool, "hotel_information", hotel_information_tool.description),
        wrap_async_tool(availability_pricing_tool, "availability_pricing", availability_pricing_tool.description),
        wrap_async_tool(guest_support_tool, "guest_support", guest_support_tool.description),
        think_tool,
        other_tool,
    ]

    if mcp_tools:
        tools.extend(mcp_tools)
        logging.info(f"🧩 {len(mcp_tools)} herramientas MCP añadidas")

    logging.info(f"🧩 Total herramientas disponibles: {[t.name for t in tools]}")
    return tools
