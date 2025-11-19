"""
🤖 Main Agent - Orquestador Principal (v6.0 - Arquitectura con Sub-Agentes + Flags anti-loop)
======================================================================================
- Evita bucles infinitos de Inciso.
- Sincroniza correctamente memoria entre herramientas.
- Integra sub-agentes: disponibilidad/precios, información general, e interno.
"""

import logging
import asyncio
from typing import Optional, List, Callable

from langchain.agents import create_openai_tools_agent, AgentExecutor
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain.tools import BaseTool

# Tools bases
from tools.think_tool import create_think_tool
from tools.inciso_tool import create_inciso_tool
from tools.sub_agent_tool_wrapper import create_sub_agent_tool

# Sub-agentes
from agents.dispo_precios_agent import DispoPreciosAgent
from agents.info_agent import InfoAgent
from agents.interno_agent import InternoAgent

# Utilidades
from core.utils.utils_prompt import load_prompt
from core.utils.time_context import get_time_context
from core.memory_manager import MemoryManager
from core.config import ModelConfig, ModelTier
from core.utils.escalation_messages import EscalationMessages


log = logging.getLogger("MainAgent")

FLAG_ESCALATION_CONFIRMATION_PENDING = "escalation_confirmation_pending"


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

        log.info("✅ MainAgent inicializado (GPT-4.1 + arquitectura modular + flags persistentes)")

    def _get_default_prompt(self) -> str:
        return (
            "Eres el agente principal de un sistema de IA para hoteles.\n\n"
            "Tu responsabilidad es ORQUESTAR: decidir qué herramienta usar según la consulta del usuario.\n\n"
            "HERRAMIENTAS DISPONIBLES:\n"
            "1. Think → consultas complejas.\n"
            "2. disponibilidad_precios → precios/reservas.\n"
            "3. base_conocimientos → servicios, políticas, info general.\n"
            "4. Inciso → mensajes intermedios.\n"
            "5. escalar_interno → escalar al encargado humano.\n\n"
            "NO generes respuestas por tu cuenta. SOLO invoca tools."
        )

    def build_tools(self, chat_id: str, hotel_name: str) -> List[BaseTool]:
        tools: List[BaseTool] = []

        tools.append(create_think_tool(model_name="gpt-4.1"))
        tools.append(create_inciso_tool(send_callback=self.send_callback))

        dispo_agent = DispoPreciosAgent(memory_manager=self.memory_manager)
        tools.append(
            create_sub_agent_tool(
                name="disponibilidad_precios",
                description=(
                    "Consulta disponibilidad, tipos de habitaciones y precios. "
                    "Úsala para reservas, fechas y tarifas."
                ),
                sub_agent=dispo_agent,
                memory_manager=self.memory_manager,
                chat_id=chat_id,
                hotel_name=hotel_name,
            )
        )

        info_agent = InfoAgent(memory_manager=self.memory_manager)
        tools.append(
            create_sub_agent_tool(
                name="base_conocimientos",
                description=(
                    "Busca información factual del hotel. Intenta primero la base de conocimientos y, "
                    "si no hay datos, recurre a Google antes de escalar."
                ),
                sub_agent=info_agent,
                memory_manager=self.memory_manager,
                chat_id=chat_id,
                hotel_name=hotel_name,
            )
        )

        if self.interno_agent:
            tools.append(
                create_sub_agent_tool(
                    name="escalar_interno",
                    description=(
                        "Escala la conversación al encargado humano. Úsala cuando falte información, "
                        "cuando el huésped lo pida o cuando sea necesaria confirmación humana."
                    ),
                    sub_agent=self.interno_agent,
                    memory_manager=self.memory_manager,
                    chat_id=chat_id,
                    hotel_name=hotel_name,
                )
            )

        log.info("🔧 Tools cargadas para %s: %s", chat_id, [t.name for t in tools])
        return tools

    def create_prompt_template(self) -> ChatPromptTemplate:
        return ChatPromptTemplate.from_messages([
            ("system", self.system_prompt),
            MessagesPlaceholder(variable_name="chat_history", optional=True),
            ("human", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])

    async def _handle_pending_confirmation(self, chat_id: str, user_input: str) -> Optional[str]:
        if not self.memory_manager:
            return None

        pending = self.memory_manager.get_flag(chat_id, FLAG_ESCALATION_CONFIRMATION_PENDING)
        if not pending:
            return None

        decision = self._interpret_confirmation(user_input)

        if decision is True:
            motivo = pending.get("reason") or "Solicitud del huésped"
            escalation_type = pending.get("escalation_type", "info_not_found")
            original_message = pending.get("guest_message") or user_input

            await self._delegate_escalation_to_interno(
                user_input=original_message,
                chat_id=chat_id,
                motivo=motivo,
                escalation_type=escalation_type,
                context="Escalación confirmada por el huésped",
            )
            return EscalationMessages.get_by_context("info")

        if decision is False:
            self.memory_manager.clear_flag(chat_id, FLAG_ESCALATION_CONFIRMATION_PENDING)
            self.memory_manager.clear_flag(chat_id, "consulta_base_realizada")
            return (
                "Perfecto, seguimos buscando alternativas sin molestar al encargado. "
                "Si quieres que lo contacte luego, solo dímelo."
            )

        return "Solo para confirmar: ¿quieres que contacte con el encargado? Responde con 'sí' o 'no'."

    def _interpret_confirmation(self, text: str) -> Optional[bool]:
        t = (text or "").strip().lower()
        if not t:
            return None

        negatives = ["prefiero que no", "mejor no", "no gracias", "no hace falta", "no por ahora", "no quiero"]
        positives = ["sí", "si", "hazlo", "adelante", "claro", "vale", "ok", "confirmo", "yes"]

        if any(n in t for n in negatives):
            return False
        if any(p in t for p in positives):
            return True
        return None

    def _request_escalation_confirmation(self, chat_id: str, user_input: str, motivo: str) -> str:
        self.memory_manager.set_flag(
            chat_id,
            FLAG_ESCALATION_CONFIRMATION_PENDING,
            {
                "guest_message": user_input,
                "reason": motivo,
                "escalation_type": "info_not_found",
            },
        )
        return (
            "Ahora mismo no tengo ese dato confirmado. "
            "¿Quieres que consulte al encargado? Responde con 'sí' o 'no'."
        )

    async def _delegate_escalation_to_interno(
            self,
            *,
            user_input: str,
            chat_id: str,
            motivo: str,
            escalation_type: str,
            context: str,
        ):
            if not self.interno_agent:
                log.error("⚠️ Se intentó escalar pero no hay InternoAgent configurado")
                return

            try:
                query = (
                    f"[ESCALATION REQUEST]\n"
                    f"Motivo: {motivo}\n"
                    f"Mensaje del huésped: {user_input}\n"
                    f"Tipo: {escalation_type}\n"
                    f"Contexto: {context}\n"
                    f"Chat ID: {chat_id}"
                )

                await self.interno_agent.ainvoke(query=query, chat_id=chat_id)

            except Exception as exc:
                log.error(f"❌ Error delegando escalación a InternoAgent: {exc}", exc_info=True)

    async def ainvoke(
        self,
        user_input: str,
        chat_id: str,
        hotel_name: str = "Hotel",
        chat_history: Optional[List] = None,
    ) -> str:

        if chat_id not in self.locks:
            self.locks[chat_id] = asyncio.Lock()

        async with self.locks[chat_id]:

            try:
                if self.memory_manager.get_flag(chat_id, "escalation_in_progress"):
                    return "##INCISO## Un momento, sigo verificando tu solicitud con el encargado."

                pending = await self._handle_pending_confirmation(chat_id, user_input)
                if pending is not None:
                    self.memory_manager.save(chat_id, "user", user_input)
                    self.memory_manager.save(chat_id, "assistant", pending)
                    return pending

                base_prompt = load_prompt("main_prompt.txt") or self._get_default_prompt()
                self.system_prompt = f"{get_time_context()}\n\n{base_prompt}"

                if chat_history is None:
                    chat_history = self.memory_manager.get_memory_as_messages(chat_id, limit=20)
                chat_history = chat_history or []

                tools = self.build_tools(chat_id, hotel_name)
                prompt_template = self.create_prompt_template()

                chain_agent = create_openai_tools_agent(
                    llm=self.llm,
                    tools=tools,
                    prompt=prompt_template,
                )

                executor = AgentExecutor(
                    agent=chain_agent,
                    tools=tools,
                    verbose=True,
                    max_iterations=25,
                    return_intermediate_steps=True,
                    max_execution_time=90,
                    handle_parsing_errors=True,
                )

                inciso_flag = self.memory_manager.get_flag(chat_id, "inciso_enviado")
                consulta_flag = self.memory_manager.get_flag(chat_id, "consulta_base_realizada")

                if consulta_flag and not self.memory_manager.get_flag(chat_id, FLAG_ESCALATION_CONFIRMATION_PENDING):
                    await self._delegate_escalation_to_interno(
                        user_input=user_input,
                        chat_id=chat_id,
                        motivo="Consulta repetida sin información",
                        escalation_type="info_not_found",
                        context="Escalación automática",
                    )
                    return EscalationMessages.get_by_context("info")

                result = await executor.ainvoke(
                    input={"input": user_input, "chat_history": chat_history},
                    config={"callbacks": []},
                )

                response = (result.get("output") or "").strip()

                if (
                    not response
                    or "no hay información disponible" in response.lower()
                    or response.upper() == "ESCALATION_REQUIRED"
                ):
                    self.memory_manager.set_flag(chat_id, "consulta_base_realizada", True)

                    if not inciso_flag and self.send_callback:
                        await self.send_callback(
                            "Dame un momento, estoy revisando internamente cómo ayudarte mejor."
                        )
                        self.memory_manager.set_flag(chat_id, "inciso_enviado", True)

                    return self._request_escalation_confirmation(
                        chat_id,
                        user_input,
                        motivo="Sin resultados en knowledge_base",
                    )

                self.memory_manager.save(chat_id, "user", user_input)
                self.memory_manager.save(chat_id, "assistant", response)

                self.memory_manager.clear_flag(chat_id, "inciso_enviado")
                self.memory_manager.clear_flag(chat_id, "consulta_base_realizada")

                return response

            except Exception as e:
                log.error(f"❌ Error en MainAgent ({chat_id}): {e}", exc_info=True)

                await self._delegate_escalation_to_interno(
                    user_input=user_input,
                    chat_id=chat_id,
                    motivo=str(e),
                    escalation_type="error",
                    context="Escalación por excepción en MainAgent",
                )
                return EscalationMessages.get_by_context("urgent")


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
