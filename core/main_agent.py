"""
🤖 Main Agent - Agente Principal Orquestador (Refactorizado y Mejorado)
=======================================================================
Este agente central coordina TODAS las interacciones del sistema.
Actúa como ORQUESTADOR, delegando tareas a las herramientas especializadas.
"""

import logging
from typing import Optional, List, Callable
from langchain_openai import ChatOpenAI
from langchain.agents import create_openai_tools_agent, AgentExecutor
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain.tools import StructuredTool

# Imports de tools
from tools.think_tool import create_think_tool
from tools.inciso_tool import create_inciso_tool
from tools.interno_tool import create_interno_tool
from tools.dispo_precios_tool import create_dispo_precios_tool
from tools.info_hotel_tool import create_info_hotel_tool

# Utilidades
from core.utils.utils_prompt import load_prompt
from core.utils.time_context import get_time_context
from core.memory_manager import MemoryManager

log = logging.getLogger("MainAgent")


class MainAgent:
    """Agente principal que orquesta todo el sistema."""

    def __init__(
        self,
        model_name: str = "gpt-4.1-mini",
        temperature: float = 0.3,
        memory_manager: Optional[MemoryManager] = None,
        send_message_callback: Optional[Callable] = None,
    ):
        self.model_name = model_name
        self.temperature = temperature
        self.memory_manager = memory_manager
        self.send_callback = send_message_callback

        # Modelo base
        self.llm = ChatOpenAI(model=self.model_name, temperature=self.temperature, streaming=False)

        # Prompt inicial con contexto temporal
        base_prompt = load_prompt("main_prompt.txt") or self._get_default_prompt()
        self.system_prompt = f"{get_time_context()}\n\n{base_prompt}"

        log.info(f"✅ MainAgent inicializado con modelo {model_name}")

    # --------------------------------------------------
    def _get_default_prompt(self) -> str:
        """Prompt por defecto si no se encuentra el archivo."""
        return """Eres el agente principal de un sistema de IA para hoteles.

Tu única responsabilidad es ORQUESTAR: decidir qué herramienta usar según la consulta del usuario.

Ya dispones de un contexto temporal actualizado con la fecha y hora actuales.
Utilízalo para interpretar correctamente expresiones como “hoy”, “mañana” o “este fin de semana”.

HERRAMIENTAS DISPONIBLES:
--------------------------
1. **Think** → Para consultas complejas o ambiguas.
2. **availability_pricing** → Para disponibilidad, precios, reservas.
3. **knowledge_base** → Para servicios, políticas, info general del hotel.
4. **Inciso** → Para enviar mensajes intermedios al usuario.
5. **Interno** → Para escalar al encargado humano (último recurso).

FLUJO DE DECISIÓN:
------------------
1. Si la consulta es ambigua → Think.
2. Si trata de precios o reservas → availability_pricing.
3. Si trata de servicios o normas → knowledge_base.
4. Si knowledge_base no responde → Inciso + Interno.
5. Usa Inciso antes de Interno.

NO generes respuestas por tu cuenta. SOLO invoca las tools adecuadas y retorna su output."""

    # --------------------------------------------------
    def _build_tools(self, chat_id: str, hotel_name: str = "Hotel") -> List[StructuredTool]:
        """Crea las herramientas disponibles para el agente principal."""
        tools = [
            create_think_tool(model_name="gpt-4.1-mini"),
            create_inciso_tool(send_callback=self.send_callback),
            create_dispo_precios_tool(memory_manager=self.memory_manager, chat_id=chat_id),
            create_info_hotel_tool(memory_manager=self.memory_manager, chat_id=chat_id),
            create_interno_tool(chat_id=chat_id, hotel_name=hotel_name),
        ]
        log.info(f"🔧 {len(tools)} herramientas configuradas para Main Agent")
        return tools

    # --------------------------------------------------
    def _create_prompt_template(self) -> ChatPromptTemplate:
        """Crea el template de prompt."""
        return ChatPromptTemplate.from_messages([
            ("system", self.system_prompt),
            MessagesPlaceholder(variable_name="chat_history", optional=True),
            ("human", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])

    # --------------------------------------------------
    def invoke(
        self,
        user_input: str,
        chat_id: str,
        hotel_name: str = "Hotel",
        chat_history: Optional[List] = None
    ) -> str:
        """Procesa una consulta del usuario."""
        try:
            log.info(f"🤖 Main Agent procesando input: {user_input[:100]}...")

            # 🕒 Actualizar contexto temporal antes de cada ejecución
            base_prompt = load_prompt("main_prompt.txt") or self._get_default_prompt()
            self.system_prompt = f"{get_time_context()}\n\n{base_prompt}"

            tools = self._build_tools(chat_id=chat_id, hotel_name=hotel_name)

            # Crear el agente
            prompt_template = self._create_prompt_template()
            agent = create_openai_tools_agent(llm=self.llm, tools=tools, prompt=prompt_template)

            # Crear el executor
            agent_executor = AgentExecutor(
                agent=agent,
                tools=tools,
                verbose=True,
                max_iterations=10,
                max_execution_time=120,
                handle_parsing_errors=True,
                return_intermediate_steps=False
            )

            # Ejecutar flujo
            result = agent_executor.invoke({
                "input": user_input,
                "chat_history": chat_history or []
            })

            # Extraer respuesta final
            response = (
                result.get("output", str(result))
                if isinstance(result, dict)
                else str(result)
            )

            log.info(f"✅ Main Agent completó ejecución ({len(response)} caracteres)")

            # 💾 Guardar conversación
            if self.memory_manager and chat_id:
                try:
                    self.memory_manager.save(chat_id, "user", user_input)
                    self.memory_manager.save(chat_id, "assistant", response)
                    log.info(f"💾 Conversación guardada en memoria ({chat_id})")
                except Exception as e:
                    log.warning(f"⚠️ No se pudo guardar la conversación: {e}")

            return response

        except Exception as e:
            log.error(f"❌ Error en Main Agent: {e}", exc_info=True)
            return (
                "❌ Ocurrió un error al procesar tu consulta. "
                "Por favor, intenta nuevamente o contacta con el hotel."
            )

    # --------------------------------------------------
    async def ainvoke(
        self,
        user_input: str,
        chat_id: str,
        hotel_name: str = "Hotel",
        chat_history: Optional[List] = None
    ) -> str:
        """Versión asíncrona."""
        return self.invoke(user_input, chat_id, hotel_name, chat_history)


# =============================================================
def create_main_agent(
    memory_manager: Optional[MemoryManager] = None,
    send_callback: Optional[Callable] = None,
    model_name: str = "gpt-4.1-mini",
    temperature: float = 0.3
) -> MainAgent:
    """Crea una instancia configurada del Main Agent."""
    return MainAgent(
        model_name=model_name,
        temperature=temperature,
        memory_manager=memory_manager,
        send_message_callback=send_callback
    )
