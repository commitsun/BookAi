import re
import logging
import asyncio
from langchain_openai import ChatOpenAI
from langchain.agents import create_openai_tools_agent, AgentExecutor
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain.tools import Tool
from core.language_manager import language_manager
from core.utils.utils_prompt import load_prompt
from core.utils.normalize_reply import normalize_reply
from core.mcp_client import mcp_client

log = logging.getLogger("InfoAgent")

ESCALATE_SENTENCE = (
    "🕓 Un momento por favor, voy a consultarlo con el encargado. "
    "Permíteme contactar con el encargado."
)

# =====================================================
# Helper: detectar si la respuesta sigue siendo un volcado técnico
# =====================================================
def _looks_like_internal_dump(text: str) -> bool:
    """
    Devuelve True solo si parece material interno sin postprocesar.
    Permitimos respuestas con 1-3 frases útiles aunque mencionen precio, horario, distancia, planta, etc.
    """
    if not text:
        return False

    # Cabeceras tipo markdown o numeraciones de manual interno
    if re.search(r"(^|\n)\s*(#{1,3}|\d+\)|\d+\.)\s", text):
        return True

    # Listas largas tipo inventario operativo interno
    dash_bullets = len(re.findall(r"\n\s*-\s", text))
    if dash_bullets >= 3:
        return True

    # Texto MUY largo => probablemente pegó el manual
    if len(text.split()) > 130:
        return True

    return False


# =====================================================
# Resumidor: mantiene la info útil, sin el tocho
# =====================================================
async def summarize_tool_output(question: str, context: str) -> str:
    """Resume la información del MCP en 1–3 frases útiles, sin volcar el manual."""
    try:
        llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.25)

        prompt = f"""
Eres Sara, la asistente del Hotel Alda Centro Ponferrada.

El huésped pregunta:
"{question}"

Esta es información interna del hotel (puede tener datos internos que no debes mostrar tal cual):
---
{context[:2500]}
---

Tu tarea:
1. Responde en español con un máximo de 3 frases claras, cálidas y profesionales.
2. Incluye datos prácticos importantes (por ejemplo: precio, horario, ubicación, restricciones).
3. No incluyas detalles operativos internos que no necesita el huésped (listados de plantas, numeraciones internas de habitaciones, inventarios técnicos).
4. No uses listas con guiones ni títulos markdown.
5. Si la información relevante NO aparece claramente, responde:
   "No dispongo de ese dato ahora mismo, pero puedo consultarlo con el encargado."
"""
        response = await llm.ainvoke(prompt)
        text = (response.content or "").strip()

        # Limpieza cosmética
        text = re.sub(r"[-*#]{1,3}\s*", "", text)
        text = re.sub(r"\s{2,}", " ", text)

        # Limitar respuesta absurda
        return text[:600]

    except Exception as e:
        log.error(f"⚠️ Error en summarize_tool_output: {e}", exc_info=True)
        return "No dispongo de ese dato ahora mismo, pero puedo consultarlo con el encargado."


# =====================================================
# Tool MCP principal
# =====================================================
async def hotel_information_tool(query: str) -> str:
    """
    Devuelve respuesta lista para el huésped a partir de la KB interna del hotel.
    Sin dumps técnicos, pero manteniendo información práctica.
    """
    try:
        q = (query or "").strip()
        if not q:
            return ESCALATE_SENTENCE

        # 1. Obtener la tool MCP
        tools = await mcp_client.get_tools(server_name="InfoAgent")
        if not tools:
            log.warning("⚠️ No se encontraron herramientas MCP para InfoAgent.")
            return ESCALATE_SENTENCE

        info_tool = next((t for t in tools if "conocimiento" in t.name.lower()), None)
        if not info_tool:
            log.warning("⚠️ No se encontró 'Base_de_conocimientos_del_hotel' en MCP.")
            return ESCALATE_SENTENCE

        # 2. Preguntar al MCP
        raw_reply = await info_tool.ainvoke({"input": q})

        # 3. Limpieza bruta (quita JSON, metadatos, etc.)
        cleaned = normalize_reply(raw_reply, q, "InfoAgent").strip()
        if not cleaned or len(cleaned) < 5:
            return ESCALATE_SENTENCE

        # 4. Resumir a un formato humano (1–3 frases)
        summarized = await summarize_tool_output(q, cleaned)

        # 5. Protección final contra dumps internos
        if _looks_like_internal_dump(summarized):
            log.warning("⚠️ Dump interno detectado → escalación automática.")
            return ESCALATE_SENTENCE

        return summarized.strip()

    except Exception as e:
        log.error(f"❌ Error en hotel_information_tool: {e}", exc_info=True)
        return ESCALATE_SENTENCE


# =====================================================
# InfoAgent (clase usada por HotelAIHybrid)
# =====================================================
class InfoAgent:
    """
    Subagente encargado de responder preguntas generales del hotel:
    - servicios
    - horarios
    - amenities
    - ubicación
    - políticas internas que afectan al huésped

    Usa 'Base_de_conocimientos_del_hotel' a través del MCP.
    """

    def __init__(self, model_name: str = "gpt-4o-mini"):
        self.model_name = model_name
        self.llm = ChatOpenAI(model=self.model_name, temperature=0.2)

        self.prompt_text = load_prompt("info_prompt.txt") or (
            "Eres el asistente informativo del hotel. "
            "Responde de forma breve, amable y precisa usando solo la información disponible. "
            "Si no sabes algo con seguridad, dilo y ofrece consultar al encargado."
        )

        # Registramos la tool "hotel_information" que llama a nuestro flow limpio
        self.tools = [self._build_tool()]

        # Agente LangChain clásico que puede invocar tools
        self.agent_executor = self._build_agent_executor()

        log.info("🏨 InfoAgent inicializado correctamente.")

    # --------------------------------------------------
    def _build_tool(self):
        return Tool(
            name="hotel_information",
            description=(
                "Responde preguntas generales del hotel (servicios, horarios, "
                "gimnasio, desayuno, parking, política de late check-out, etc.)."
            ),
            func=lambda q: self._sync_run(hotel_information_tool, q),
            coroutine=hotel_information_tool,
            return_direct=True,
        )

    # --------------------------------------------------
    def _build_agent_executor(self):
        prompt = ChatPromptTemplate.from_messages([
            ("system", self.prompt_text),
            MessagesPlaceholder("chat_history"),
            ("user", "{input}"),
            MessagesPlaceholder("agent_scratchpad"),
        ])
        agent = create_openai_tools_agent(self.llm, self.tools, prompt)
        return AgentExecutor(agent=agent, tools=self.tools, verbose=True)

    # --------------------------------------------------
    def _sync_run(self, coro, *args, **kwargs):
        """
        Ejecuta una coroutine async desde un contexto sync (langchain Tool.func lo necesita).
        Maneja tanto caso normal como entorno con loop ya activo (FastAPI / uvicorn / WhatsApp buffer).
        """
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        if loop.is_running():
            import nest_asyncio
            nest_asyncio.apply()

        return loop.run_until_complete(coro(*args, **kwargs))

    # --------------------------------------------------
    async def handle(self, pregunta: str) -> str:
        """
        Punto de entrada público usado por HotelAIHybrid.
        Devuelve texto final listo para mandar al huésped por WhatsApp.
        """
        log.info(f"📩 [InfoAgent] Pregunta huésped: {pregunta}")
        lang = language_manager.detect_language(pregunta)

        try:
            result = await self.agent_executor.ainvoke({
                "input": pregunta.strip(),
                "chat_history": [],
            })

            # LangChain a veces devuelve con distintas keys
            output = (
                result.get("output")
                or result.get("final_output")
                or result.get("response")
                or ""
            )

            respuesta_final = language_manager.ensure_language(output.strip(), lang)

            # Último cinturón de seguridad: si por lo que sea
            # el agente devolvió un párrafo gigante, escalamos.
            if _looks_like_internal_dump(respuesta_final):
                return ESCALATE_SENTENCE

            log.info(f"✅ [InfoAgent] Respuesta final al huésped: {respuesta_final[:200]}")
            return (
                respuesta_final
                or "No dispongo de ese dato ahora mismo, pero puedo consultarlo con el encargado."
            )

        except Exception as e:
            log.error(f"💥 Error en InfoAgent.handle: {e}", exc_info=True)
            return "Ha ocurrido un problema al obtener la información del hotel."
