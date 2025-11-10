import logging
import datetime
import asyncio
from langchain_openai import ChatOpenAI
from langchain.agents import create_openai_tools_agent, AgentExecutor
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain.tools import Tool

# Core imports
from core.mcp_client import call_availability_pricing  # 👈 nuevo método HTTP
from core.language_manager import language_manager
from core.utils.normalize_reply import normalize_reply
from core.utils.utils_prompt import load_prompt
from core.utils.time_context import get_time_context

log = logging.getLogger("DispoPreciosAgent")


class DispoPreciosAgent:
    """
    Subagente encargado de responder preguntas sobre disponibilidad y precios.
    Ahora usa directamente el MCP Server HTTP (sin MCP client).
    """

    def __init__(self, model_name: str = "gpt-4.1-mini", memory_manager=None):
        self.model_name = model_name
        self.llm = ChatOpenAI(model=self.model_name, temperature=0.2)
        self.memory_manager = memory_manager

        base_prompt = load_prompt("dispo_precios_prompt.txt") or self._get_default_prompt()
        self.prompt_text = f"{get_time_context()}\n\n{base_prompt.strip()}"

        self.tools = [self._build_tool()]
        self.agent_executor = self._build_agent_executor()

        log.info("💰 DispoPreciosAgent inicializado correctamente (HTTP local).")

    # ----------------------------------------------------------
    def _get_default_prompt(self) -> str:
        """Prompt por defecto si no existe el archivo."""
        return (
            "Eres un agente especializado en disponibilidad y precios de un hotel.\n"
            "Tu función es responder con precisión sobre fechas, precios y tipos de habitación disponibles.\n\n"
            "Usa la información proporcionada por el PMS (Roomdoo) y responde con tono amable y profesional.\n"
            "Si la información no es suficiente, solicita detalles adicionales al huésped (fechas, número de personas, etc.)."
        )

    # ----------------------------------------------------------
    def _build_tool(self):
        """Tool HTTP directa que consulta disponibilidad y precios en el PMS."""
        async def _availability_tool(query: str):
            try:
                # Fechas por defecto (si el huésped no especifica)
                today = datetime.date.today()
                checkin_date = today + datetime.timedelta(days=7)
                checkout_date = checkin_date + datetime.timedelta(days=2)

                checkin = f"{checkin_date}T00:00:00"
                checkout = f"{checkout_date}T00:00:00"
                occupancy = 2
                pms_property_id = 38  # demo

                # 👇 Llamada directa al MCP Server local
                result = await call_availability_pricing(
                    checkin=checkin,
                    checkout=checkout,
                    occupancy=occupancy,
                    pms_property_id=pms_property_id,
                )

                if not result or "error" in result:
                    log.error(f"❌ Error desde availability_pricing: {result}")
                    return "No dispongo de disponibilidad en este momento."

                rooms = result.get("data") or result.get("response") or []
                if not rooms:
                    return "No hay disponibilidad para esas fechas."

                # 🧩 Construir respuesta directamente con los datos reales (sin LLM)
                lines = []
                for r in rooms:
                    name = r.get("roomTypeName", "Habitación")
                    avail = r.get("avail", 0)
                    price = r.get("price", "?")
                    lines.append(
                        f"- {name}: {price} € por noche ({avail} hab. disponibles)"
                    )

                header = (
                    f"Para las fechas del {checkin_date.strftime('%d/%m/%Y')} "
                    f"al {checkout_date.strftime('%d/%m/%Y')} para {occupancy} personas, "
                    f"tenemos estas opciones:\n\n"
                )

                respuesta = header + "\n".join(lines)
                log.info(f"✅ [DispoPreciosAgent/tool] Respuesta factual: {respuesta}")
                return respuesta

            except Exception as e:
                log.error(f"❌ Error en _availability_tool: {e}", exc_info=True)
                return "Ha ocurrido un problema al consultar precios o disponibilidad."

        return Tool(
            name="availability_pricing",
            func=lambda q: self._sync_run(_availability_tool, q),
            description="Consulta disponibilidad, precios y tipos de habitación del hotel (HTTP local).",
            return_direct=True,
        )

    # ----------------------------------------------------------
    def _build_agent_executor(self):
        """Crea el AgentExecutor de LangChain."""
        prompt = ChatPromptTemplate.from_messages([
            ("system", self.prompt_text),
            MessagesPlaceholder("chat_history"),
            ("user", "{input}"),
            MessagesPlaceholder("agent_scratchpad"),
        ])

        agent = create_openai_tools_agent(self.llm, self.tools, prompt)

        return AgentExecutor(
            agent=agent,
            tools=self.tools,
            verbose=True,
            return_intermediate_steps=False,
            handle_parsing_errors=True,
            max_iterations=4,
            max_execution_time=60
        )

    # ----------------------------------------------------------
    def _sync_run(self, coro, *args, **kwargs):
        """Permite ejecutar async coroutines dentro de contextos sync."""
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
    async def handle(self, pregunta: str, chat_history=None, chat_id: str = None) -> str:
        """Entrada principal del subagente (modo asíncrono)."""
        log.info(f"📩 [DispoPreciosAgent] Recibida pregunta: {pregunta}")
        lang = language_manager.detect_language(pregunta)

        try:
            base_prompt = load_prompt("dispo_precios_prompt.txt") or self._get_default_prompt()
            self.prompt_text = f"{get_time_context()}\n\n{base_prompt.strip()}"

            result = await self.agent_executor.ainvoke({
                "input": pregunta.strip(),
                "chat_history": chat_history or [],
            })

            output = next(
                (result.get(k) for k in ["output", "final_output", "response"] if result.get(k)),
                ""
            )

            respuesta_final = normalize_reply(
                language_manager.ensure_language(output, lang),
                pregunta,
                agent_name="DispoPreciosAgent"
            )

            if self.memory_manager and chat_id:
                self.memory_manager.update_memory(
                    chat_id,
                    role="assistant",
                    content=f"[DispoPreciosAgent] Entrada: {pregunta}\n\nRespuesta: {respuesta_final}"
                )

            log.info(f"✅ [DispoPreciosAgent] Respuesta final: {respuesta_final[:200]}")
            return respuesta_final or "No dispongo de disponibilidad en este momento."

        except Exception as e:
            log.error(f"❌ Error en DispoPreciosAgent: {e}", exc_info=True)
            if self.memory_manager and chat_id:
                self.memory_manager.update_memory(
                    chat_id,
                    role="system",
                    content=f"[DispoPreciosAgent] Error interno: {e}"
                )
            return "Ha ocurrido un problema al obtener la disponibilidad."

    # ----------------------------------------------------------
    def invoke(self, user_input: str, chat_history=None, chat_id: str = None) -> str:
        """Versión síncrona (wrapper) para integración con DispoPreciosTool."""
        try:
            return self._sync_run(self.handle, user_input, chat_history, chat_id)
        except Exception as e:
            log.error(f"❌ Error en DispoPreciosAgent.invoke: {e}", exc_info=True)
            if self.memory_manager and chat_id:
                self.memory_manager.update_memory(
                    chat_id,
                    role="system",
                    content=f"[DispoPreciosAgent] Error en invocación síncrona: {e}"
                )
            return "Ha ocurrido un error al procesar la disponibilidad o precios."
