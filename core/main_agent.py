"""
🤖 Main Agent - Orquestador Principal
====================================
- Coordina TODAS las interacciones del sistema.
- Actúa como orquestador: decide qué tool usar.
- Incluye:
  - Corte de loops infinitos
  - Detección de necesidad de escalación
  - Persistencia en memoria
"""

import logging
from typing import Optional, List, Callable
from langchain_openai import ChatOpenAI
from langchain.agents import create_openai_tools_agent, AgentExecutor
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain.tools import StructuredTool

# Tools del sistema
from tools.think_tool import create_think_tool
from tools.inciso_tool import create_inciso_tool
from tools.interno_tool import create_interno_tool
from tools.dispo_precios_tool import create_dispo_precios_tool
from tools.info_hotel_tool import create_info_hotel_tool

# Utilidades
from core.utils.utils_prompt import load_prompt
from core.utils.time_context import get_time_context
from core.memory_manager import MemoryManager
from agents.interno_agent import InternoAgent

log = logging.getLogger("MainAgent")

# =============================================================
# 🚦 Control de loops por chat
# =============================================================
AGENT_ACTIVE = {}


class MainAgent:
    """Agente principal que orquesta todas las operaciones del sistema."""

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

        # LLM base
        self.llm = ChatOpenAI(
            model=self.model_name,
            temperature=self.temperature,
            streaming=False
        )

        # Prompt base (dinámico + fallback)
        base_prompt = load_prompt("main_prompt.txt") or self._get_default_prompt()
        self.system_prompt = f"{get_time_context()}\n\n{base_prompt}"

        # Agente humano interno (Telegram)
        self.interno_agent = InternoAgent()
        log.info(f"✅ MainAgent inicializado con modelo {model_name}")

    # --------------------------------------------------
    def _get_default_prompt(self) -> str:
        """Prompt por defecto si no se encuentra el archivo."""
        return (
            "Eres el agente principal de un sistema de IA para hoteles.\n\n"
            "Tu única responsabilidad es ORQUESTAR: decidir qué herramienta usar según la consulta del usuario.\n\n"
            "Ya dispones de un contexto temporal actualizado con la fecha y hora actuales. "
            "Utilízalo para interpretar correctamente expresiones como “hoy”, “mañana” o “este fin de semana”.\n\n"
            "HERRAMIENTAS DISPONIBLES:\n"
            "1. Think → consultas complejas o ambiguas.\n"
            "2. availability_pricing → disponibilidad, precios, reservas.\n"
            "3. knowledge_base → servicios, políticas, info general del hotel.\n"
            "4. Inciso → mensajes intermedios.\n"
            "5. Interno → escalar al encargado humano (último recurso).\n\n"
            "FLUJO DE DECISIÓN:\n"
            "1. Si la consulta es ambigua → Think.\n"
            "2. Si trata de precios o reservas → availability_pricing.\n"
            "3. Si trata de servicios o normas → knowledge_base.\n"
            "4. Si knowledge_base no responde → Inciso + Interno.\n"
            "5. Usa Inciso antes de Interno.\n\n"
            "NO generes respuestas por tu cuenta. SOLO invoca las tools adecuadas y retorna su output."
        )

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
        log.info(f"🔧 {len(tools)} herramientas configuradas para MainAgent ({chat_id})")
        return tools

    # --------------------------------------------------
    def _create_prompt_template(self) -> ChatPromptTemplate:
        """Crea el template de prompt para el agente."""
        return ChatPromptTemplate.from_messages([
            ("system", self.system_prompt),
            MessagesPlaceholder(variable_name="chat_history", optional=True),
            ("human", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])

    # --------------------------------------------------
    async def ainvoke(
        self,
        user_input: str,
        chat_id: str,
        hotel_name: str = "Hotel",
        chat_history: Optional[List] = None
    ) -> str:
        """
        Procesa la consulta del usuario de forma asíncrona.
        Devuelve SIEMPRE texto listo para mandar al huésped.
        """
        try:
            # 🚦 Bloquea loops reentrantes
            if AGENT_ACTIVE.get(chat_id):
                log.warning(f"⚠️ Loop detectado para chat {chat_id}. Deteniendo ejecución temprana.")
                # Marcamos explícitamente como INCISO para que el caller no lo audite ni lo escale
                return "##INCISO## Estoy verificando tu solicitud con el sistema interno, un momento por favor."

            AGENT_ACTIVE[chat_id] = True
            log.info(f"🤖 MainAgent procesando input de {chat_id}: {user_input[:200]}...")

            # 🕒 Refrescar prompt con contexto temporal actual
            base_prompt = load_prompt("main_prompt.txt") or self._get_default_prompt()
            self.system_prompt = f"{get_time_context()}\n\n{base_prompt}"

            # 🧩 Construir agente tool-based
            tools = self._build_tools(chat_id, hotel_name)
            prompt_template = self._create_prompt_template()

            chain_agent = create_openai_tools_agent(
                llm=self.llm,
                tools=tools,
                prompt=prompt_template
            )

            executor = AgentExecutor(
                agent=chain_agent,
                tools=tools,
                verbose=True,
                max_iterations=6,
                max_execution_time=90,
                handle_parsing_errors=True,
                return_intermediate_steps=False
            )

            # 🚀 Ejecutar
            result = executor.invoke({
                "input": user_input,
                "chat_history": chat_history or []
            })

            # result normalmente es dict con "output"
            response = (
                result.get("output", str(result))
                if isinstance(result, dict)
                else str(result)
            ).strip()

            log.info(f"🧠 Respuesta bruta del agente ({chat_id}): {response[:500]}")

            # 🧠 AUTO-ESCALACIÓN: si las tools no han podido resolver
            if "ESCALATION_REQUIRED" in response or "ESCALAR_A_INTERNO" in response:
                log.warning("🚨 Señal de escalación detectada dentro de la respuesta del agente.")
                await self.interno_agent.anotify_staff(
                    (
                        "Consulta escalada automáticamente:\n\n"
                        f"User input:\n{user_input}\n\n"
                        f"Contexto chat_id: {chat_id}"
                    ),
                    chat_id=chat_id,
                    context={"motivo": "Sin respuesta clara / información insuficiente"}
                )
                return (
                    "##INCISO## Estoy verificando esa información con nuestro equipo. "
                    "Te respondo enseguida."
                )

            # 💾 Guardar conversación
            if self.memory_manager and chat_id:
                try:
                    self.memory_manager.save(chat_id, "user", user_input)
                    self.memory_manager.save(chat_id, "assistant", response)
                    log.info(f"💾 Conversación guardada correctamente ({chat_id})")
                except Exception as e:
                    log.warning(f"⚠️ No se pudo guardar la conversación ({chat_id}): {e}")

            log.info(f"✅ MainAgent completó ejecución para {chat_id} ({len(response)} chars)")
            return response

        except Exception as e:
            log.error(f"❌ Error en MainAgent para chat {chat_id}: {e}", exc_info=True)
            return (
                "##INCISO## Ha ocurrido un problema técnico al procesar tu solicitud. "
                "Estoy comprobando la información con el equipo."
            )

        finally:
            # liberar lock anti-loop
            AGENT_ACTIVE.pop(chat_id, None)


# =============================================================
def create_main_agent(
    memory_manager: Optional[MemoryManager] = None,
    send_callback: Optional[Callable] = None,
    model_name: str = "gpt-4.1-mini",
    temperature: float = 0.3
) -> MainAgent:
    """Factory para crear el MainAgent configurado."""
    return MainAgent(
        model_name=model_name,
        temperature=temperature,
        memory_manager=memory_manager,
        send_message_callback=send_callback
    )
