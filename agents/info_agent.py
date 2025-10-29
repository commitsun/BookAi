"""
📚 InfoAgent v2 - Subagente de información del hotel
=====================================================
Subagente especializado en responder preguntas generales
sobre el hotel: servicios, horarios, políticas, ubicación, etc.

Este agente es invocado desde la tool `info_hotel_tool.py`
dentro del flujo orquestado del Main Agent.
"""

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
from core.utils.time_context import get_time_context


log = logging.getLogger("InfoAgent")

ESCALATE_SENTENCE = (
    "🕓 Un momento por favor, voy a consultarlo con el encargado. "
    "Permíteme contactar con el encargado."
)


# =====================================================
# 🔍 Helper: detectar si parece volcado técnico interno
# =====================================================
def _looks_like_internal_dump(text: str) -> bool:
    if not text:
        return False
    if re.search(r"(^|\n)\s*(#{1,3}|\d+\)|\d+\.)\s", text):
        return True
    if len(re.findall(r"\n\s*-\s", text)) >= 3:
        return True
    if len(text.split()) > 130:
        return True
    return False


# =====================================================
# 🧠 Resumen limpio del contexto interno
# =====================================================
async def summarize_tool_output(question: str, context: str) -> str:
    """Resume la información técnica en 1–3 frases útiles para el huésped."""
    try:
        llm = ChatOpenAI(model="gpt-4.1-mini", temperature=0.25)

        prompt = f"""
Eres el asistente del hotel.

El huésped pregunta:
"{question}"

Esta es información interna del hotel (puede tener datos técnicos):
---
{context[:2500]}
---

Tu tarea:
1. Resume en máximo 3 frases claras, cálidas y profesionales.
2. Menciona datos útiles (horarios, precios, ubicación, servicios).
3. No muestres información interna o técnica.
4. Si no hay datos suficientes, di:
   "No dispongo de ese dato ahora mismo, pero puedo consultarlo con el encargado."
"""

        response = await llm.ainvoke(prompt)
        text = (response.content or "").strip()
        text = re.sub(r"[-*#]{1,3}\s*", "", text)
        text = re.sub(r"\s{2,}", " ", text)
        return text[:600]

    except Exception as e:
        log.error(f"⚠️ Error en summarize_tool_output: {e}", exc_info=True)
        return "No dispongo de ese dato ahora mismo, pero puedo consultarlo con el encargado."


# =====================================================
# 🧩 Tool principal (consulta MCP)
# =====================================================
async def hotel_information_tool(query: str) -> str:
    """
    Devuelve respuesta procesada desde la base de conocimientos (MCP).
    """
    try:
        q = (query or "").strip()
        if not q:
            return ESCALATE_SENTENCE

        tools = await mcp_client.get_tools(server_name="InfoAgent")
        if not tools:
            log.warning("⚠️ No se encontraron herramientas MCP para InfoAgent.")
            return ESCALATE_SENTENCE

        info_tool = next((t for t in tools if "conocimiento" in t.name.lower()), None)
        if not info_tool:
            log.warning("⚠️ No se encontró 'Base_de_conocimientos_del_hotel' en MCP.")
            return ESCALATE_SENTENCE

        raw_reply = await info_tool.ainvoke({"input": q})
        cleaned = normalize_reply(raw_reply, q, "InfoAgent").strip()
        if not cleaned or len(cleaned) < 5:
            return ESCALATE_SENTENCE

        summarized = await summarize_tool_output(q, cleaned)
        if _looks_like_internal_dump(summarized):
            log.warning("⚠️ Dump interno detectado → escalación automática.")
            return ESCALATE_SENTENCE

        return summarized.strip()

    except Exception as e:
        log.error(f"❌ Error en hotel_information_tool: {e}", exc_info=True)
        return ESCALATE_SENTENCE


# =====================================================
# 🏨 Clase InfoAgent
# =====================================================
class InfoAgent:
    """
    Subagente que responde preguntas generales sobre el hotel.
    Se invoca desde la tool `info_hotel_tool.py`.
    """

    def __init__(self, model_name: str = "gpt-4.1-mini"):
        self.model_name = model_name
        self.llm = ChatOpenAI(model=self.model_name, temperature=0.2)

        # 🕒 Prompt inicial con contexto temporal dinámico
        base_prompt = load_prompt("info_hotel_prompt.txt") or self._get_default_prompt()
        self.prompt_text = f"{get_time_context()}\n\n{base_prompt.strip()}"

        # 🔧 Inicializar herramientas y executor
        self.tools = [self._build_tool()]
        self.agent_executor = self._build_agent_executor()

        log.info("✅ InfoAgent inicializado correctamente.")

    # --------------------------------------------------
    def _get_default_prompt(self) -> str:
        """Prompt por defecto si no se encuentra el archivo."""
        return (
            "Eres un asistente especializado en información del hotel.\n"
            "Respondes preguntas sobre servicios, horarios, políticas, ubicación y amenities.\n\n"
            "Tu tono es profesional, amable y conciso. Si no tienes la información exacta,\n"
            "informa al huésped de que consultarás con el encargado."
        )

    # --------------------------------------------------
    def _build_tool(self):
        return Tool(
            name="hotel_information",
            description="Responde preguntas sobre servicios, horarios, amenities o políticas del hotel.",
            func=lambda q: self._sync_run(hotel_information_tool, q),
            coroutine=hotel_information_tool,
            return_direct=True,
        )

    # --------------------------------------------------
    def _build_agent_executor(self):
        """Crea el AgentExecutor con el prompt actualizado."""
        prompt = ChatPromptTemplate.from_messages([
            ("system", self.prompt_text),
            MessagesPlaceholder("chat_history"),
            ("user", "{input}"),
            MessagesPlaceholder("agent_scratchpad"),
        ])
        agent = create_openai_tools_agent(self.llm, self.tools, prompt)
        return AgentExecutor(agent=agent, tools=self.tools, verbose=False)

    # --------------------------------------------------
    def _sync_run(self, coro, *args, **kwargs):
        """Permite ejecutar coroutines async desde un entorno sync."""
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
    async def invoke(self, user_input: str, chat_history: list = None) -> str:
        """
        Punto de entrada unificado (usado por la tool `info_hotel_tool`).
        """
        log.info(f"📩 [InfoAgent] Consulta: {user_input}")
        lang = language_manager.detect_language(user_input)
        chat_history = chat_history or []

        try:
            # 🕒 Actualizar contexto temporal dinámicamente en cada ejecución
            base_prompt = load_prompt("info_hotel_prompt.txt") or self._get_default_prompt()
            self.prompt_text = f"{get_time_context()}\n\n{base_prompt.strip()}"

            result = await self.agent_executor.ainvoke({
                "input": user_input.strip(),
                "chat_history": chat_history,
            })

            output = (
                result.get("output")
                or result.get("final_output")
                or result.get("response")
                or ""
            ).strip()

            respuesta_final = language_manager.ensure_language(output, lang)

            if _looks_like_internal_dump(respuesta_final):
                log.warning("⚠️ Respuesta detectada como dump interno → escalación.")
                return "ESCALAR_A_INTERNO"

            log.info(f"✅ [InfoAgent] Respuesta final: {respuesta_final[:200]}")
            return respuesta_final or ESCALATE_SENTENCE

        except Exception as e:
            log.error(f"💥 Error en InfoAgent.invoke: {e}", exc_info=True)
            return ESCALATE_SENTENCE
