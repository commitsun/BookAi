import logging
import json
import datetime
from langchain_openai import ChatOpenAI
from langchain.agents import create_openai_tools_agent, AgentExecutor
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain.tools import Tool
from core.mcp_client import mcp_client
from core.language_manager import language_manager
from core.utils.normalize_reply import normalize_reply
from core.utils.utils_prompt import load_prompt

log = logging.getLogger("DispoPreciosAgent")


class DispoPreciosAgent:
    """
    Subagente encargado de responder preguntas sobre disponibilidad,
    tipos de habitación, precios y reservas.
    Usa las tools 'buscar_token' y 'Disponibilidad_y_precios' del MCP.
    """

    def __init__(self, model_name: str = "gpt-4.1-mini"):
        self.model_name = model_name
        self.llm = ChatOpenAI(model=self.model_name, temperature=0.2)

        # Prompt específico del agente de precios/disponibilidad
        self.prompt_text = load_prompt("dispo_precios_prompt.txt") or (
            "Eres el asistente de reservas del hotel. "
            "Responde de forma amable, clara y breve usando solo los datos de disponibilidad y precios que tengas."
        )

        # Tools + agent
        self.tools = [self._build_tool()]
        self.agent_executor = self._build_agent_executor()
        log.info("💰 DispoPreciosAgent inicializado correctamente.")

    # ----------------------------------------------------------
    def _build_tool(self):
        async def _availability_tool(query: str):
            try:
                tools = await mcp_client.get_tools(server_name="DispoPreciosAgent")
                token_tool = next((t for t in tools if t.name == "buscar_token"), None)
                dispo_tool = next((t for t in tools if t.name == "Disponibilidad_y_precios"), None)

                if not token_tool or not dispo_tool:
                    return "No dispongo de disponibilidad en este momento."

                # Obtener token
                token_raw = await token_tool.ainvoke({})
                token_data = json.loads(token_raw) if isinstance(token_raw, str) else token_raw
                token = (
                    token_data[0].get("key") if isinstance(token_data, list)
                    else token_data.get("key")
                )
                if not token:
                    return "No se pudo obtener el token de acceso."

                # Fechas por defecto (7 días a partir de hoy)
                today = datetime.date.today()
                checkin = today + datetime.timedelta(days=7)
                checkout = checkin + datetime.timedelta(days=2)

                params = {
                    "checkin": f"{checkin}T00:00:00",
                    "checkout": f"{checkout}T00:00:00",
                    "occupancy": 2,
                    "key": token,
                }

                raw_reply = await dispo_tool.ainvoke(params)
                rooms = json.loads(raw_reply) if isinstance(raw_reply, str) else raw_reply

                if not rooms or not isinstance(rooms, list):
                    return "No hay disponibilidad en las fechas indicadas."

                prompt = f"""
Información de habitaciones y precios (€/noche):

{json.dumps(rooms, ensure_ascii=False, indent=2)}

El huésped pregunta: "{query}"
"""
                response = await self.llm.ainvoke(prompt)
                return response.content.strip()

            except Exception as e:
                log.error(f"❌ Error en availability_pricing_tool: {e}", exc_info=True)
                return "Ha ocurrido un problema al consultar precios o disponibilidad."

        return Tool(
            name="availability_pricing",
            func=lambda q: self._sync_run(_availability_tool, q),
            description="Consulta disponibilidad, precios y tipos de habitación del hotel.",
            return_direct=True,
        )

    # ----------------------------------------------------------
    def _build_agent_executor(self):
        """Crea el AgentExecutor con verbose=True para mantener logs visibles."""
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
        """Permite ejecutar async coroutines dentro de contextos sync (LangChain)."""
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
        log.info(f"📩 [DispoPreciosAgent] Recibida pregunta: {pregunta}")
        lang = language_manager.detect_language(pregunta)

        try:
            result = await self.agent_executor.ainvoke({
                "input": pregunta.strip(),
                "chat_history": [],
            })
            output = next((result.get(k) for k in ["output", "final_output", "response"] if result.get(k)), "")
            respuesta_final = language_manager.ensure_language(output, lang)
            log.info(f"✅ [DispoPreciosAgent] Respuesta final: {respuesta_final[:200]}")
            return respuesta_final or "No dispongo de disponibilidad en este momento."

        except Exception as e:
            log.error(f"Error en DispoPreciosAgent: {e}", exc_info=True)
            return "Ha ocurrido un problema al obtener la disponibilidad."
