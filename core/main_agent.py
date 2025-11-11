"""
🤖 Main Agent - Orquestador Principal (v5.1 - con control de incisos y flags anti-loop)
======================================================================================
- Evita bucles infinitos de Inciso.
- Evita búsquedas duplicadas en la base de conocimiento.
- Acompaña al cliente con mensajes intermedios controlados.
"""

import logging
import asyncio
from typing import Optional, List, Callable
from langchain.agents import create_openai_tools_agent, AgentExecutor
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain.tools import StructuredTool

# Tools del sistema
from tools.think_tool import create_think_tool
from tools.inciso_tool import create_inciso_tool
from tools.interno_tool import create_interno_tools
from tools.dispo_precios_tool import create_dispo_precios_tool
from tools.info_hotel_tool import create_info_hotel_tool

# Utilidades
from core.utils.utils_prompt import load_prompt
from core.utils.time_context import get_time_context
from core.memory_manager import MemoryManager
from core.config import ModelConfig, ModelTier
from core.utils.escalation_messages import EscalationMessages
from agents.interno_agent import InternoAgent

log = logging.getLogger("MainAgent")


class MainAgent:
    """Agente principal que orquesta todas las operaciones del sistema."""

    def __init__(
        self,
        memory_manager: Optional[MemoryManager] = None,
        send_message_callback: Optional[Callable] = None,
        interno_agent: Optional[InternoAgent] = None,
    ):
        self.llm = ModelConfig.get_llm(ModelTier.MAIN)
        self.memory_manager = memory_manager
        self.send_callback = send_message_callback
        self.interno_agent = interno_agent
        self.locks = {}

        base_prompt = load_prompt("main_prompt.txt") or self._get_default_prompt()
        self.system_prompt = f"{get_time_context()}\n\n{base_prompt}"
        log.info("✅ MainAgent inicializado (GPT-4.1 + Flags persistentes + Anti-loop)")

    # --------------------------------------------------
    def _get_default_prompt(self) -> str:
        return (
            "Eres el agente principal de un sistema de IA para hoteles.\n\n"
            "Tu responsabilidad es ORQUESTAR: decidir qué herramienta usar según la consulta del usuario.\n\n"
            "HERRAMIENTAS DISPONIBLES:\n"
            "1. Think → consultas complejas.\n"
            "2. availability_pricing → precios/reservas.\n"
            "3. knowledge_base → servicios, políticas, info general.\n"
            "4. Inciso → mensajes intermedios.\n"
            "5. Interno → escalar al encargado humano (último recurso).\n\n"
            "FLUJO DE DECISIÓN:\n"
            "1. Si la consulta es ambigua → Think.\n"
            "2. Si trata de precios o reservas → availability_pricing.\n"
            "3. Si trata de servicios → knowledge_base.\n"
            "4. Si knowledge_base no responde → Inciso + Interno.\n"
            "5. Usa Inciso antes de Interno.\n\n"
            "NO generes respuestas por tu cuenta. SOLO invoca tools y retorna su output."
        )

    # --------------------------------------------------
    def _build_tools(self, chat_id: str, hotel_name: str = "Hotel") -> List[StructuredTool]:
        tools = [
            create_think_tool(model_name="gpt-4.1"),
            create_inciso_tool(send_callback=self.send_callback),
            create_dispo_precios_tool(memory_manager=self.memory_manager, chat_id=chat_id),
            create_info_hotel_tool(memory_manager=self.memory_manager, chat_id=chat_id),
        ]
        log.info(f"🔧 {len(tools)} herramientas cargadas para MainAgent ({chat_id})")
        return tools

    # --------------------------------------------------
    def _create_prompt_template(self) -> ChatPromptTemplate:
        return ChatPromptTemplate.from_messages([
            ("system", self.system_prompt),
            MessagesPlaceholder(variable_name="chat_history", optional=True),
            ("human", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])

    # --------------------------------------------------
    async def _clear_escalation_flag_later(self, chat_id: str, delay: int = 90):
        await asyncio.sleep(delay)
        self.memory_manager.clear_flag(chat_id, "escalation_in_progress")
        log.info(f"🧹 Escalation flag limpiado para {chat_id}")

    # --------------------------------------------------
    async def ainvoke(
        self,
        user_input: str,
        chat_id: str,
        hotel_name: str = "Hotel",
        chat_history: Optional[List] = None
    ) -> str:
        if chat_id not in self.locks:
            self.locks[chat_id] = asyncio.Lock()

        async with self.locks[chat_id]:
            try:
                if self.memory_manager.get_flag(chat_id, "escalation_in_progress"):
                    log.warning(f"⚠️ Escalación ya en curso para {chat_id}")
                    return "##INCISO## Un momento, sigo verificando tu solicitud con el encargado."

                log.info(f"🤖 Procesando input de {chat_id}: {user_input[:200]}")

                # Actualizar prompt contextual
                base_prompt = load_prompt("main_prompt.txt") or self._get_default_prompt()
                self.system_prompt = f"{get_time_context()}\n\n{base_prompt}"

                # Construcción de herramientas
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
                    max_iterations=25,  # reducido para evitar loops largos
                    max_execution_time=90,
                    handle_parsing_errors=True,
                    return_intermediate_steps=False
                )

                # =============================================================
                # 🚫 Prevención de loops y consultas repetidas
                # =============================================================
                inciso_flag = self.memory_manager.get_flag(chat_id, "inciso_enviado")
                consulta_flag = self.memory_manager.get_flag(chat_id, "consulta_base_realizada")

                # Si ya se consultó sin éxito, escalar directamente
                if consulta_flag:
                    log.info("🔁 Consulta base ya realizada sin resultados. Escalando directamente.")
                    await self._escalar_a_encargado(user_input, chat_id, motivo="Consulta repetida sin información")
                    return EscalationMessages.get_by_context("info")

                # =============================================================
                # 🚀 Invocar ejecución principal
                # =============================================================
                result = await executor.ainvoke({
                    "input": user_input,
                    "chat_history": chat_history or []
                })

                response = (
                    result.get("output", str(result))
                    if isinstance(result, dict)
                    else str(result)
                ).strip()

                log.info(f"🧠 Output del agente ({chat_id}): {response[:400]}")

                # =============================================================
                # 🔍 Detección de búsquedas vacías o requerimiento de escalación
                # =============================================================
                if (
                    not response
                    or "no aparece ninguna mención" in response.lower()
                    or "no hay información disponible" in response.lower()
                ):
                    log.warning(f"⚠️ No se encontró información útil para {chat_id}")
                    self.memory_manager.set_flag(chat_id, "consulta_base_realizada", True)

                    # Si no se ha enviado inciso, enviar uno breve
                    if not inciso_flag:
                        self.memory_manager.set_flag(chat_id, "inciso_enviado", True)
                        if self.send_callback:
                            await self.send_callback("Un momento, voy a verificar con el encargado para confirmarte. 😊")

                    await self._escalar_a_encargado(user_input, chat_id, motivo="Sin resultados en knowledge_base")
                    return EscalationMessages.get_by_context("info")

                # =============================================================
                # 💾 Guardar conversación
                # =============================================================
                if self.memory_manager:
                    try:
                        self.memory_manager.save(chat_id, "user", user_input)
                        self.memory_manager.save(chat_id, "assistant", response)
                        log.info(f"💾 Conversación guardada correctamente ({chat_id})")
                    except Exception as e:
                        log.warning(f"⚠️ No se pudo guardar la conversación: {e}")

                # Limpieza de flags si la respuesta fue correcta
                self.memory_manager.clear_flag(chat_id, "inciso_enviado")
                self.memory_manager.clear_flag(chat_id, "consulta_base_realizada")

                return response

            except Exception as e:
                log.error(f"❌ Error en MainAgent ({chat_id}): {e}", exc_info=True)
                await self._escalar_a_encargado(user_input, chat_id, motivo=str(e))
                return EscalationMessages.get_by_context("urgent")

    # --------------------------------------------------
    async def _escalar_a_encargado(self, user_input: str, chat_id: str, motivo: str):
        """Encapsula la lógica de escalación con flags y logs consistentes."""
        try:
            self.memory_manager.set_flag(chat_id, "escalation_in_progress", True)
            log.warning(f"🚨 Escalando conversación ({chat_id}) → motivo: {motivo}")
            await self.interno_agent.escalate(
                guest_chat_id=chat_id,
                guest_message=user_input,
                escalation_type="info_not_found",
                reason=motivo,
                context=f"Auto-escalación iniciada por MainAgent"
            )
            asyncio.create_task(self._clear_escalation_flag_later(chat_id))
        except Exception as e:
            log.error(f"⚠️ Fallo en la escalación interna: {e}", exc_info=True)
# =============================================================
# ✅ Factory retrocompatible
# =============================================================
def create_main_agent(
    memory_manager: Optional[MemoryManager] = None,
    send_callback: Optional[Callable] = None,
    interno_agent: Optional[InternoAgent] = None,
) -> MainAgent:
    return MainAgent(
        memory_manager=memory_manager,
        send_message_callback=send_callback,
        interno_agent=interno_agent,
    )
