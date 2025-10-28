import logging
from langchain_openai import ChatOpenAI
from langchain.agents import create_openai_tools_agent, AgentExecutor
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from core.language_manager import language_manager
from core.utils.utils_prompt import load_prompt
from core.utils.normalize_reply import normalize_reply
from core.mcp_client import mcp_client
from langchain_core.messages import HumanMessage, AIMessage
from langchain.tools import Tool

log = logging.getLogger("InfoAgent")


class InfoAgent:
    """
    Subagente encargado de responder preguntas generales del hotel:
    servicios, horarios, amenities, ubicación, políticas, etc.
    Usa la tool 'Base_de_conocimientos_del_hotel' del MCP.
    """

    def __init__(self, model_name: str = "gpt-4.1-mini"):
        self.model_name = model_name
        self.llm = ChatOpenAI(model=self.model_name, temperature=0.2)

        # Prompt específico
        self.prompt_text = load_prompt("info_prompt.txt") or (
            "Eres el asistente informativo del hotel. "
            "Responde de forma breve, amable y precisa usando solo la información disponible."
        )

        # Build tool + agent
        self.tools = [self._build_tool()]
        self.agent_executor = self._build_agent_executor()
        log.info("🏨 InfoAgent inicializado correctamente.")

    # ----------------------------------------------------------
    def _build_tool(self):
        """Tool MCP: Base_de_conocimientos_del_hotel"""
        async def _info_tool(query: str):
            try:
                tools = await mcp_client.get_tools(server_name="InfoAgent")
                info_tool = next((t for t in tools if t.name == "Base_de_conocimientos_del_hotel"), None)
                if not info_tool:
                    return "No dispongo de información de ese tema en este momento."

                raw = await info_tool.ainvoke({"input": query})
                cleaned = normalize_reply(raw, query, "InfoAgent")
                return cleaned or "No dispongo de ese dato en este momento."

            except Exception as e:
                log.error(f"❌ Error ejecutando InfoAgent tool: {e}", exc_info=True)
                return "Ha ocurrido un problema al consultar la información del hotel."

        return Tool(
            name="hotel_information",
            func=lambda q: self._sync_run(_info_tool, q),
            description="Responde preguntas generales sobre el hotel: servicios, horarios, amenities, ubicación o políticas.",
            return_direct=True,
        )

    # ----------------------------------------------------------
    def _build_agent_executor(self):
        """Crea el AgentExecutor con verbose=True para mostrar logs."""
        prompt = ChatPromptTemplate.from_messages([
            ("system", self.prompt_text),
            MessagesPlaceholder("chat_history"),
            ("user", "{input}"),
            MessagesPlaceholder("agent_scratchpad"),
        ])
        agent = create_openai_tools_agent(self.llm, self.tools, prompt)
        return AgentExecutor(agent=agent, tools=self.tools, verbose=True)

    # ----------------------------------------------------------
    def _sync_run(self, coro, *args, **kwargs):
        """Ejecuta funciones async dentro de contextos sync."""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        if loop.is_running():
            import nest_asyncio
            nest_asyncio.apply()
        return loop.run_until_complete(coro(*args, **kwargs))

    # ----------------------------------------------------------
    async def handle(self, pregunta: str) -> str:
        """Entrada principal del subagente."""
        log.info(f"📩 [InfoAgent] Recibida pregunta: {pregunta}")
        lang = language_manager.detect_language(pregunta)

        try:
            result = await self.agent_executor.ainvoke({
                "input": pregunta.strip(),
                "chat_history": [],
            })
            output = next((result.get(k) for k in ["output", "final_output", "response"] if result.get(k)), "")
            respuesta_final = language_manager.ensure_language(output, lang)
            log.info(f"✅ [InfoAgent] Respuesta final: {respuesta_final[:200]}")
            return respuesta_final or "No dispongo de ese dato en este momento."

        except Exception as e:
            log.error(f"Error en InfoAgent: {e}", exc_info=True)
            return "Ha ocurrido un problema al obtener la información del hotel."
