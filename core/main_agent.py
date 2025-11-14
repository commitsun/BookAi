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
from pydantic import BaseModel, Field

# Tools del sistema
from tools.think_tool import create_think_tool
from tools.inciso_tool import create_inciso_tool
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

FLAG_ESCALATION_CONFIRMATION_PENDING = "escalation_confirmation_pending"


# =============================================================
# 🧠 Esquema pydantic para tool Interno
# =============================================================
class InternoEscalationInput(BaseModel):
    motivo: str = Field(..., description="Motivo resumido para el encargado humano.")
    mensaje_cliente: str = Field(..., description="Mensaje original o resumen del huésped.")
    tipo: str = Field(
        default="info_not_found",
        description="Tipo de escalación (info_not_found, inappropriate, bad_response, etc.)",
    )


# =============================================================
# 🧠 MainAgent
# =============================================================
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
            "5. Interno → escalar al encargado humano.\n\n"
            "NO generes respuestas por tu cuenta. SOLO invoca tools."
        )

    # --------------------------------------------------
    def _build_tools(self, chat_id: str, hotel_name: str = "Hotel") -> List[StructuredTool]:
        tools = [
            create_think_tool(model_name="gpt-4.1"),
            create_inciso_tool(send_callback=self.send_callback),
            create_dispo_precios_tool(memory_manager=self.memory_manager, chat_id=chat_id),
            create_info_hotel_tool(memory_manager=self.memory_manager, chat_id=chat_id),
        ]

        # =============================================================
        # 🧩 Agregar herramienta "Interno" si está habilitado
        # =============================================================
        if self.interno_agent:

            async def _interno_coroutine(motivo: str, mensaje_cliente: str, tipo: str = "info_not_found") -> str:
                mensaje = (mensaje_cliente or "").strip() or motivo
                tipo_normalized = (tipo or "info_not_found").strip() or "info_not_found"

                await self._delegate_escalation_to_interno(
                    user_input=mensaje,
                    chat_id=chat_id,
                    motivo=motivo,
                    escalation_type=tipo_normalized,
                    context="Escalación solicitada manualmente vía herramienta Interno",
                )
                return EscalationMessages.get_by_context("info")

            def _interno_sync(**kwargs) -> str:
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)

                if loop.is_running():
                    import nest_asyncio
                    nest_asyncio.apply()

                return loop.run_until_complete(_interno_coroutine(**kwargs))

            tools.append(
                StructuredTool(
                    name="Interno",
                    description=(
                        "Escala la conversación al encargado humano por Telegram. "
                        "Úsala solo cuando el huésped lo pida explícitamente o cuando tú lo confirmes."
                    ),
                    func=_interno_sync,
                    coroutine=_interno_coroutine,
                    args_schema=InternoEscalationInput,
                )
            )

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
    async def _handle_pending_confirmation(self, chat_id: str, user_input: str) -> Optional[str]:
        if not self.memory_manager:
            return None

        pending = self.memory_manager.get_flag(chat_id, FLAG_ESCALATION_CONFIRMATION_PENDING)
        if not pending:
            return None

        decision = self._interpret_confirmation(user_input)

        # Cliente confirma → escalar
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

        # Cliente rechaza
        if decision is False:
            self.memory_manager.clear_flag(chat_id, FLAG_ESCALATION_CONFIRMATION_PENDING)
            self.memory_manager.clear_flag(chat_id, "consulta_base_realizada")
            return (
                "Perfecto, seguimos buscando alternativas sin molestar al encargado. "
                "Si cambias de opinión, avísame y lo contacto al momento."
            )

        # No hay confirmación clara
        return (
            "Solo para confirmar, ¿quieres que contacte con el encargado respecto a tu consulta? "
            "Respóndeme con 'sí' o 'no'."
        )

    # --------------------------------------------------
    def _interpret_confirmation(self, text: str) -> Optional[bool]:
        normalized = (text or "").strip().lower()
        if not normalized:
            return None

        negative = [
            "prefiero que no", "mejor no", "no gracias", "no hace falta",
            "todavia no", "todavía no", "aun no", "aún no",
            "no por ahora", "espera", "no quiero", "stop",
        ]

        if any(p in normalized for p in negative):
            return False

        positive = [
            "sí", "si", "hazlo", "adelante", "claro", "por favor",
            "vale", "ok", "okay", "confirmo", "yes", "go ahead",
        ]

        if any(p in normalized for p in positive):
            return True

        words = set(normalized.split())
        if words & {"si", "sí", "ok", "vale", "yes"}:
            return True
        if words & {"no"}:
            return False

        return None

    # --------------------------------------------------
    def _request_escalation_confirmation(self, chat_id: str, user_input: str, motivo: str) -> str:
        if self.memory_manager:
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
            "¿Quieres que se lo pregunte al encargado del hotel? "
            "Respóndeme con 'sí' para que lo contacte o 'no' para seguir buscando alternativas."
        )

    # --------------------------------------------------
    async def _delegate_escalation_to_interno(
        self,
        *,
        user_input: str,
        chat_id: str,
        motivo: str,
        escalation_type: str = "info_not_found",
        context: str = "Auto-escalación iniciada por MainAgent",
    ):
        if not self.interno_agent:
            log.error("⚠️ Se intentó escalar pero no existe InternoAgent configurado")
            return

        try:
            await self.interno_agent.handle_guest_escalation(
                chat_id=chat_id,
                guest_message=(user_input or "").strip() or motivo,
                reason=motivo,
                escalation_type=escalation_type,
                context=context,
                confirmation_flag=FLAG_ESCALATION_CONFIRMATION_PENDING,
            )
        except Exception as exc:
            log.error(f"⚠️ Falló la escalación ({chat_id}): {exc}", exc_info=True)

    # --------------------------------------------------
    async def ainvoke(
        self,
        user_input: str,
        chat_id: str,
        hotel_name: str = "Hotel",
        chat_history: Optional[List] = None,
    ) -> str:

        # Locks para evitar condiciones de carrera
        if chat_id not in self.locks:
            self.locks[chat_id] = asyncio.Lock()

        async with self.locks[chat_id]:
            try:
                # Escalación en curso → bloquear respuestas
                if self.memory_manager and self.memory_manager.get_flag(chat_id, "escalation_in_progress"):
                    log.warning(f"⚠️ Escalación ya en curso para {chat_id}")
                    return "##INCISO## Un momento, sigo verificando tu solicitud con el encargado."

                log.info(f"🤖 Procesando input de {chat_id}: {user_input[:200]}")

                # ¿Hay una confirmación pendiente?
                pending_response = await self._handle_pending_confirmation(chat_id, user_input)
                if pending_response is not None:
                    if self.memory_manager:
                        try:
                            self.memory_manager.save(chat_id, "user", user_input)
                            self.memory_manager.save(chat_id, "assistant", pending_response)
                        except Exception as e:
                            log.warning(f"⚠️ No se pudo guardar conversación en confirmación: {e}")
                    return pending_response

                # Refrescar prompt contextual
                base_prompt = load_prompt("main_prompt.txt") or self._get_default_prompt()
                self.system_prompt = f"{get_time_context()}\n\n{base_prompt}"

                # Construcción de herramientas
                tools = self._build_tools(chat_id, hotel_name)
                prompt_template = self._create_prompt_template()

                chain_agent = create_openai_tools_agent(
                    llm=self.llm, tools=tools, prompt=prompt_template
                )

                executor = AgentExecutor(
                    agent=chain_agent,
                    tools=tools,
                    verbose=True,
                    max_iterations=25,
                    max_execution_time=90,
                    handle_parsing_errors=True,
                    return_intermediate_steps=False,
                )

                # FLAGS anti-loop
                inciso_flag = (
                    self.memory_manager.get_flag(chat_id, "inciso_enviado")
                    if self.memory_manager else False
                )
                consulta_flag = (
                    self.memory_manager.get_flag(chat_id, "consulta_base_realizada")
                    if self.memory_manager else False
                )

                # Si ya se consultó sin éxito → escalar directamente
                if consulta_flag and not self.memory_manager.get_flag(chat_id, FLAG_ESCALATION_CONFIRMATION_PENDING):
                    log.info("🔁 Consulta base ya realizada sin resultados. Escalando directamente.")

                    await self._delegate_escalation_to_interno(
                        user_input=user_input,
                        chat_id=chat_id,
                        motivo="Consulta repetida sin información",
                        context="Escalación automática tras búsquedas sin resultado",
                    )
                    return EscalationMessages.get_by_context("info")

                # --------------------------------------------------
                # Ejecutar agente
                # --------------------------------------------------
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

                # --------------------------------------------------
                # Sin info útil → solicitar confirmación
                # --------------------------------------------------
                if (
                    not response
                    or "no aparece ninguna mención" in response.lower()
                    or "no hay información disponible" in response.lower()
                    or response.strip().upper() == "ESCALATION_REQUIRED"
                ):
                    log.warning(f"⚠️ No se encontró información útil ({chat_id})")

                    if self.memory_manager:
                        self.memory_manager.set_flag(chat_id, "consulta_base_realizada", True)

                    # Enviar inciso (si no ha sido enviado antes)
                    if not inciso_flag and self.send_callback:
                        await self.send_callback(
                            "Dame un momento, estoy revisando internamente cómo ayudarte mejor."
                        )
                        if self.memory_manager:
                            self.memory_manager.set_flag(chat_id, "inciso_enviado", True)

                    confirmation_message = self._request_escalation_confirmation(
                        chat_id, user_input, motivo="Sin resultados en knowledge_base",
                    )
                    return confirmation_message

                # --------------------------------------------------
                # Guardar conversación
                # --------------------------------------------------
                if self.memory_manager:
                    try:
                        self.memory_manager.save(chat_id, "user", user_input)
                        self.memory_manager.save(chat_id, "assistant", response)
                        log.info(f"💾 Conversación guardada ({chat_id})")
                    except Exception as e:
                        log.warning(f"⚠️ No se pudo guardar la conversación: {e}")

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


# =============================================================
# Factory retrocompatible
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
