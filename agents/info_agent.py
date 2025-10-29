"""
📚 InfoAgent v2 - Subagente de información del hotel (con escalación automática)
===============================================================================
Responde preguntas generales sobre el hotel: servicios, horarios, políticas, etc.
Si no encuentra información clara → Escala automáticamente al encargado vía Telegram.
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
from agents.interno_agent import InternoAgent  # 👈 se añade

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

Información interna del hotel:
---
{context[:2500]}
---

Tu tarea:
1. Frases útiles y amables.
2. No muestres información técnica o confidencial.
3. Si no hay datos útiles, di:
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
    Escala automáticamente al encargado si no hay información útil.
    """

    def __init__(self, model_name: str = "gpt-4.1-mini"):
        self.model_name = model_name
        self.llm = ChatOpenAI(model=self.model_name, temperature=0.2)
        self.interno_agent = InternoAgent()  # 👈 se añade

        base_prompt = load_prompt("info_hotel_prompt.txt") or self._get_default_prompt()
        self.prompt_text = f"{get_time_context()}\n\n{base_prompt.strip()}"
        self.tools = [self._build_tool()]
        self.agent_executor = self._build_agent_executor()

        log.info("✅ InfoAgent inicializado correctamente.")

    # --------------------------------------------------
    def _get_default_prompt(self) -> str:
        return (
            "Eres un asistente especializado en información del hotel.\n"
            "Respondes preguntas sobre servicios, horarios, políticas, ubicación y amenities.\n"
            "Si no tienes la información exacta, consulta con el encargado."
        )

    # --------------------------------------------------
    def _build_tool(self):
        return Tool(
            name="hotel_information",
            description="Responde preguntas sobre servicios, horarios o políticas del hotel.",
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
        return AgentExecutor(agent=agent, tools=self.tools, verbose=False)

    # --------------------------------------------------
    def _sync_run(self, coro, *args, **kwargs):
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
    async def invoke(self, user_input: str, chat_history: list = None, chat_id: str = None) -> str:
        """
        Punto de entrada unificado (usado por la tool `info_hotel_tool`).
        Si no hay información → Escalación automática.
        """
        log.info(f"📩 [InfoAgent] Consulta: {user_input}")
        lang = language_manager.detect_language(user_input)
        chat_history = chat_history or []

        try:
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

            # 🔎 Detección de falta de información
            no_info = any(
                p in respuesta_final.lower()
                for p in [
                    "no dispongo", "no tengo información", "no sé",
                    "consultarlo con el encargado", "permíteme contactar"
                ]
            )

            if _looks_like_internal_dump(respuesta_final) or no_info or respuesta_final == ESCALATE_SENTENCE:
                log.warning("⚠️ Escalación automática: no se encontró información útil.")
                msg = (
                    f"❓ *Consulta del huésped:*\n{user_input}\n\n"
                    "🧠 *Contexto:*\nEl sistema no encontró información relevante en la base de conocimiento."
                )
                self.interno_agent.notify_staff(msg, chat_id, context={"tipo": "info_no_encontrada"})
                return "ESCALATION_REQUIRED"

            log.info(f"✅ [InfoAgent] Respuesta final: {respuesta_final[:200]}")
            return respuesta_final or ESCALATE_SENTENCE

        except Exception as e:
            log.error(f"💥 Error en InfoAgent.invoke: {e}", exc_info=True)
            self.interno_agent.notify_staff(
                f"Error en InfoAgent al procesar:\n{user_input}",
                chat_id,
                context={"tipo": "error_runtime"}
            )
            return "ESCALATION_REQUIRED"
