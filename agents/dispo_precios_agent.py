import logging
import json
import datetime
import asyncio
from langchain_openai import ChatOpenAI
from langchain.agents import create_openai_tools_agent, AgentExecutor
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain.tools import Tool

# Core imports
from core.mcp_client import mcp_client
from core.language_manager import language_manager
from core.utils.normalize_reply import normalize_reply
from core.utils.utils_prompt import load_prompt
from core.utils.time_context import get_time_context  # 🕒 Contexto temporal global

log = logging.getLogger("DispoPreciosAgent")


class DispoPreciosAgent:
    """
    Subagente encargado de responder preguntas sobre disponibilidad,
    tipos de habitación, precios y reservas.
    Usa las tools 'buscar_token' y 'Disponibilidad_y_precios' del MCP.
    Integrado con MemoryManager para trazabilidad completa.
    """

    def __init__(self, model_name: str = "gpt-4.1-mini", memory_manager=None):
        self.model_name = model_name
        self.llm = ChatOpenAI(model=self.model_name, temperature=0.2)
        self.memory_manager = memory_manager

        # 🧩 Construcción inicial del prompt con contexto temporal
        base_prompt = load_prompt("dispo_precios_prompt.txt") or self._get_default_prompt()
        self.prompt_text = f"{get_time_context()}\n\n{base_prompt.strip()}"

        # Inicialización de tools y agente
        self.tools = [self._build_tool()]
        self.agent_executor = self._build_agent_executor()

        log.info("💰 DispoPreciosAgent inicializado correctamente con memoria y contexto temporal.")

    # ----------------------------------------------------------
    def _get_default_prompt(self) -> str:
        """Prompt por defecto si no existe el archivo en disco."""
        return (
            "Eres un agente especializado en disponibilidad y precios de un hotel.\n"
            "Tu función es responder con precisión sobre fechas, precios y tipos de habitación disponibles.\n\n"
            "Usa la información proporcionada por el PMS y responde con tono amable y profesional.\n"
            "Si la información no es suficiente, solicita detalles adicionales al huésped (fechas, número de personas, etc.)."
        )

    # ----------------------------------------------------------
    def _build_tool(self):
        """Crea la tool que consulta disponibilidad y precios en el PMS vía MCP."""
        async def _availability_tool(query: str):
            try:
                tools = await mcp_client.get_tools(server_name="DispoPreciosAgent")
                token_tool = next((t for t in tools if t.name == "buscar_token"), None)
                dispo_tool = next((t for t in tools if t.name == "Disponibilidad_y_precios"), None)

                if not token_tool or not dispo_tool:
                    log.warning("⚠️ No se encontraron las tools necesarias en MCP.")
                    return "No dispongo de disponibilidad en este momento."

                # Obtener token de acceso
                token_raw = await token_tool.ainvoke({})
                token_data = json.loads(token_raw) if isinstance(token_raw, str) else token_raw
                token = (
                    token_data[0].get("key") if isinstance(token_data, list)
                    else token_data.get("key")
                )

                if not token:
                    log.error("❌ No se pudo obtener el token de acceso.")
                    return "No se pudo obtener el token de acceso."

                # Fechas por defecto: dentro de 7 días, estancia de 2 noches
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

                prompt = (
                    f"{get_time_context()}\n\n"
                    f"Información de habitaciones y precios (€/noche):\n\n"
                    f"{json.dumps(rooms, ensure_ascii=False, indent=2)}\n\n"
                    f"El huésped pregunta: \"{query}\""
                )

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
        """Crea el AgentExecutor con control de iteraciones y sin pasos intermedios."""
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
        """Permite ejecutar async coroutines dentro de contextos sync (LangChain)."""
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
        """Entrada principal del subagente (modo asíncrono) con soporte de memoria."""
        log.info(f"📩 [DispoPreciosAgent] Recibida pregunta: {pregunta}")
        lang = language_manager.detect_language(pregunta)

        try:
            # 🔁 Refrescar contexto temporal antes de cada ejecución
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

            raw_output = language_manager.ensure_language(output, lang)
            respuesta_final = normalize_reply(raw_output, pregunta, agent_name="DispoPreciosAgent")

            # 🧹 Limpieza de duplicados y redundancias
            seen, cleaned = set(), []
            for line in respuesta_final.splitlines():
                line = line.strip()
                if line and line not in seen:
                    cleaned.append(line)
                    seen.add(line)

            respuesta_final = " ".join(cleaned).strip()

            # 🧠 Guardar interacción en memoria
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
