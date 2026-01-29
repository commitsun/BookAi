"""
🤖 Main Agent - Orquestador Principal (v6.0 - Arquitectura con Sub-Agentes + Flags anti-loop)
======================================================================================
- Evita bucles infinitos de Inciso.
- Sincroniza correctamente memoria entre herramientas.
- Integra sub-agentes: disponibilidad/precios, información general, e interno.
"""

import logging
import asyncio
import unicodedata
import re
from typing import Optional, List, Callable

from langchain.agents import create_openai_tools_agent, AgentExecutor
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain.tools import BaseTool

# Tools bases
from tools.think_tool import create_think_tool
from tools.inciso_tool import create_inciso_tool
from tools.sub_agent_tool_wrapper import create_sub_agent_tool
from tools.property_context_tool import create_property_context_tool

# Sub-agentes
from agents.dispo_precios_agent import DispoPreciosAgent
from agents.info_agent import InfoAgent
from agents.interno_agent import InternoAgent
from agents.onboarding_agent import OnboardingAgent

# Utilidades
from core.utils.utils_prompt import load_prompt
from core.utils.time_context import get_time_context
from core.utils.dynamic_context import build_dynamic_context_from_memory
from core.memory_manager import MemoryManager
from core.config import ModelConfig, ModelTier
from core.utils.escalation_messages import EscalationMessages


log = logging.getLogger("MainAgent")

FLAG_ESCALATION_CONFIRMATION_PENDING = "escalation_confirmation_pending"
FLAG_PROPERTY_CONFIRMATION_PENDING = "property_confirmation_pending"
FLAG_PROPERTY_DISAMBIGUATION_PENDING = "property_disambiguation_pending"


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
        self.system_prompt = f"{get_time_context()}\n\n{base_prompt.strip()}"

        log.info("✅ MainAgent inicializado (GPT-4.1 + arquitectura modular + flags persistentes)")

    def _get_default_prompt(self) -> str:
        return (
            "Eres el agente principal de un sistema de IA para hoteles.\n\n"
            "Tu responsabilidad es ORQUESTAR: decidir qué herramienta usar según la consulta del usuario.\n\n"
            "HERRAMIENTAS DISPONIBLES:\n"
            "1. Think → consultas complejas.\n"
            "2. disponibilidad_precios → precios y disponibilidad.\n"
            "3. base_conocimientos → servicios, políticas, info general.\n"
            "4. Inciso → mensajes intermedios.\n"
            "5. identificar_property → fija el contexto de la propiedad.\n"
            "6. escalar_interno → escalar al encargado humano.\n\n"
            "NO generes respuestas por tu cuenta. SOLO invoca tools."
        )

    def build_tools(self, chat_id: str, hotel_name: str) -> List[BaseTool]:
        tools: List[BaseTool] = []

        tools.append(create_think_tool(model_name="gpt-4.1"))
        tools.append(create_inciso_tool(send_callback=self.send_callback))
        tools.append(create_property_context_tool(memory_manager=self.memory_manager, chat_id=chat_id))

        dispo_agent = DispoPreciosAgent(memory_manager=self.memory_manager)
        tools.append(
            create_sub_agent_tool(
                name="disponibilidad_precios",
                description=(
                    "Consulta disponibilidad, tipos de habitaciones y precios. "
                    "Úsala para fechas, tarifas y tipos de habitación."
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

        onboarding_agent = OnboardingAgent(memory_manager=self.memory_manager)
        tools.append(
            create_sub_agent_tool(
                name="onboarding_reservas",
                description=(
                    "Gestiona reservas completas: obtiene token, identifica roomTypeId, crea la reserva "
                    "y consulta reservas propias del huésped. Úsala cuando el huésped quiera confirmar "
                    "una reserva con datos concretos o revisar su reserva."
                ),
                sub_agent=onboarding_agent,
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

    def _needs_property_context(self, chat_id: str) -> bool:
        if not self.memory_manager or not chat_id:
            return False
        return not self.memory_manager.get_flag(chat_id, "property_id")

    def _get_property_candidates(self, chat_id: str) -> list[dict]:
        if not self.memory_manager or not chat_id:
            return []
        candidates = self.memory_manager.get_flag(chat_id, "property_disambiguation_candidates") or []
        if isinstance(candidates, list):
            return candidates
        return []

    def _normalize_text(self, value: str) -> str:
        text = (value or "").strip().lower()
        if not text:
            return ""
        text = unicodedata.normalize("NFKD", text)
        text = "".join(ch for ch in text if not unicodedata.combining(ch))
        text = re.sub(r"[^a-z0-9]+", " ", text).strip()
        return re.sub(r"\s+", " ", text)

    def _tokenize(self, value: str) -> list[str]:
        text = self._normalize_text(value)
        if not text:
            return []
        stop = {"hotel", "hostal", "aldea", "alda", "el", "la", "los", "las", "de", "del"}
        return [t for t in text.split() if t and t not in stop]

    def _load_embedded_prompt(self, key: str) -> str:
        """
        Carga snippets embebidos dentro de main_prompt.txt usando marcadores:
        [[KEY]] ... [[/KEY]]
        """
        try:
            base_prompt = load_prompt("main_prompt.txt") or ""
        except Exception:
            base_prompt = ""
        if not base_prompt or not key:
            return ""
        pattern = rf"\\[\\[{re.escape(key)}\\]\\](.*?)\\[\\[/{re.escape(key)}\\]\\]"
        match = re.search(pattern, base_prompt, flags=re.DOTALL | re.IGNORECASE)
        if not match:
            return ""
        return match.group(1).strip()

    def _build_disambiguation_question(self, candidates: list[dict]) -> str:
        prompt = self._load_embedded_prompt("PROPERTY_DISAMBIGUATION")
        if prompt:
            return prompt
        return "¿En cuál de nuestros hoteles estarías interesado? Indícame el nombre exacto, por favor."

    def _resolve_property_from_candidates(self, chat_id: str, user_input: str) -> bool:
        if not self.memory_manager or not chat_id:
            return False
        raw = (user_input or "").strip()
        if not raw:
            return False
        candidates = self._get_property_candidates(chat_id)
        if not candidates:
            return False

        selected = None
        lowered = raw.lower()
        raw_tokens = set(self._tokenize(raw))
        best_score = -1
        best_cand = None
        for cand in candidates:
            name = (cand or {}).get("name") or ""
            code = (cand or {}).get("hotel_code") or ""
            if name and (name.lower() in lowered or lowered in name.lower()):
                selected = cand
                break
            if code and (code.lower() in lowered or lowered in code.lower()):
                selected = cand
                break
            if name and raw_tokens:
                cand_tokens = set(self._tokenize(name))
                overlap = len(raw_tokens & cand_tokens)
                if overlap > best_score:
                    best_score = overlap
                    best_cand = cand

        if not selected and best_cand and best_score > 0:
            selected = best_cand

        if not selected and raw.isdigit():
            for cand in candidates:
                if str(cand.get("property_id") or "").strip() == raw:
                    selected = cand
                    break

        if not selected:
            return False

        try:
            tool = create_property_context_tool(memory_manager=self.memory_manager, chat_id=chat_id)
            tool.invoke(
                {
                    "hotel_code": selected.get("hotel_code") or selected.get("name"),
                    "property_id": selected.get("property_id"),
                }
            )
        except Exception as exc:
            log.warning("No se pudo fijar property desde candidatos: %s", exc)
            return False

        return bool(
            self.memory_manager.get_flag(chat_id, "property_id")
            or self.memory_manager.get_flag(chat_id, "property_name")
        )

    async def _resolve_property_from_message(self, chat_id: str, user_input: str) -> bool:
        if not self.memory_manager or not chat_id:
            return False

        raw = (user_input or "").strip()
        if not raw:
            return False

        property_id = None
        if raw.isdigit():
            try:
                property_id = int(raw)
            except Exception:
                property_id = None

        try:
            tool = create_property_context_tool(memory_manager=self.memory_manager, chat_id=chat_id)
            tool.invoke(
                {
                    "hotel_code": None if property_id is not None else raw,
                    "property_id": property_id,
                }
            )
        except Exception as exc:
            log.warning("No se pudo resolver property desde mensaje: %s", exc)
            return False

        return bool(
            self.memory_manager.get_flag(chat_id, "property_id")
            or self.memory_manager.get_flag(chat_id, "property_name")
        )

    def _request_property_context(self, chat_id: str, original_message: str) -> str:
        self.memory_manager.set_flag(
            chat_id,
            FLAG_PROPERTY_CONFIRMATION_PENDING,
            {"original_message": original_message},
        )
        prompt = self._load_embedded_prompt("PROPERTY_REQUEST")
        if prompt:
            return prompt
        return "¿En qué hotel o propiedad te gustaría alojarte?"

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

                await self.interno_agent.ainvoke(user_input=query, chat_id=chat_id)

            except Exception as exc:
                log.error(f"❌ Error delegando escalación a InternoAgent: {exc}", exc_info=True)

    async def ainvoke(
        self,
        user_input: str,
        chat_id: str,
        hotel_name: str = "Hotel",
        chat_history: Optional[List] = None,
    ) -> str:

        if not self.memory_manager:
            raise RuntimeError("MemoryManager no configurado en MainAgent")

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

                candidates = self._get_property_candidates(chat_id)
                pending_disambiguation = self.memory_manager.get_flag(chat_id, FLAG_PROPERTY_DISAMBIGUATION_PENDING)
                if pending_disambiguation:
                    resolved = self._resolve_property_from_candidates(chat_id, user_input)
                    if not resolved:
                        question = self._build_disambiguation_question(candidates)
                        self.memory_manager.save(chat_id, "user", user_input)
                        self.memory_manager.save(chat_id, "assistant", question)
                        return question
                    self.memory_manager.clear_flag(chat_id, FLAG_PROPERTY_DISAMBIGUATION_PENDING)
                    self.memory_manager.clear_flag(chat_id, "property_disambiguation_candidates")
                    self.memory_manager.clear_flag(chat_id, "property_disambiguation_hotel_code")
                    self.memory_manager.save(chat_id, "user", user_input)
                    if isinstance(pending_disambiguation, dict):
                        original_message = pending_disambiguation.get("original_message")
                        if original_message:
                            user_input = original_message

                if candidates and not self.memory_manager.get_flag(chat_id, "property_id"):
                    question = self._build_disambiguation_question(candidates)
                    self.memory_manager.set_flag(
                        chat_id,
                        FLAG_PROPERTY_DISAMBIGUATION_PENDING,
                        {"original_message": user_input},
                    )
                    self.memory_manager.save(chat_id, "user", user_input)
                    self.memory_manager.save(chat_id, "assistant", question)
                    return question

                pending_property = self.memory_manager.get_flag(chat_id, FLAG_PROPERTY_CONFIRMATION_PENDING)
                if pending_property:
                    resolved = await self._resolve_property_from_message(chat_id, user_input)
                    if not resolved:
                        prompt = self._load_embedded_prompt("PROPERTY_REQUEST")
                        question = prompt or "¿Podrías decirme el nombre del hotel en el que quieres alojarte?"
                        self.memory_manager.save(chat_id, "user", user_input)
                        self.memory_manager.save(chat_id, "assistant", question)
                        return question
                    self.memory_manager.clear_flag(chat_id, FLAG_PROPERTY_CONFIRMATION_PENDING)
                    original_message = pending_property.get("original_message") if isinstance(pending_property, dict) else None
                    self.memory_manager.save(chat_id, "user", user_input)
                    if original_message:
                        user_input = original_message

                if self._needs_property_context(chat_id):
                    question = self._request_property_context(chat_id, user_input)
                    self.memory_manager.save(chat_id, "user", user_input)
                    self.memory_manager.save(chat_id, "assistant", question)
                    return question

                base_prompt = load_prompt("main_prompt.txt") or self._get_default_prompt()
                dynamic_context = build_dynamic_context_from_memory(self.memory_manager, chat_id)
                if dynamic_context:
                    self.system_prompt = (
                        f"{get_time_context()}\n\n{base_prompt.strip()}\n\n{dynamic_context}"
                    )
                else:
                    self.system_prompt = f"{get_time_context()}\n\n{base_prompt.strip()}"

                if chat_history is None:
                    chat_history = self.memory_manager.get_memory_as_messages(chat_id, limit=30)
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
                fallback_msg = (
                    "Ha ocurrido un problema interno y ya lo estoy revisando con el encargado. "
                    "Te aviso en breve."
                )

                # Guarda el intercambio aunque haya error para no perder contexto
                try:
                    if self.memory_manager:
                        self.memory_manager.save(chat_id, "user", user_input)
                        self.memory_manager.save(chat_id, "assistant", fallback_msg)
                except Exception:
                    log.debug("No se pudo guardar en memoria tras excepción", exc_info=True)

                # Mensaje determinista → evita duplicados por variaciones aleatorias
                return fallback_msg


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
