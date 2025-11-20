"""
InternoAgent v7 - Sub-agente Independiente con sincronización de memoria
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import datetime
from typing import Any, List, Optional

from langchain.agents import AgentExecutor, create_openai_tools_agent
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder

from core.config import ModelConfig, ModelTier
from core.utils.time_context import get_time_context
from core.utils.utils_prompt import load_prompt
from tools.interno_tool import (
    ESCALATIONS_STORE,
    Escalation,
    create_interno_tools,
    send_to_encargado,
)

log = logging.getLogger("InternoAgent")


class InternoAgent:
    """Agente interno independiente con creación de executor por invocación."""

    def __init__(
        self,
        memory_manager: Optional[Any] = None,
        escalation_db: Optional[Any] = None,
        channel_manager: Optional[Any] = None,
        model_tier: ModelTier = ModelTier.INTERNAL,
    ) -> None:
        self.memory_manager = memory_manager
        self.escalation_db = escalation_db
        self.channel_manager = channel_manager
        self.model_tier = model_tier
        self.escalations = ESCALATIONS_STORE

        self.llm = ModelConfig.get_llm(model_tier)
        log.info("InternoAgent inicializado (modelo: %s)", self.llm.model_name)

    async def ainvoke(
        self,
        user_input: str,
        chat_id: str,
        escalation_id: Optional[str] = None,
        escalation_context: str = "",
        context_window: int = 15,
        chat_history: Optional[List[Any]] = None,
        escalation_payload: Optional[dict[str, Any]] = None,
        auto_notify: bool = False,
    ) -> str:

        pre_notified = False
        notify_result: Optional[str] = None

        try:
            log.info("[InternoAgent] ainvoke inicio: %s - chat_id: %s", escalation_id, chat_id)

            # --- Cargar memoria ---
            if chat_history is None and self.memory_manager:
                chat_history = await self._safe_call(
                    getattr(self.memory_manager, "get_memory_as_messages", None),
                    conversation_id=chat_id,
                    limit=context_window,
                )
            chat_history = chat_history or []

            # --- Tools ---
            tools = create_interno_tools()

            # --- Prompt ---
            system_prompt = self._build_system_prompt(escalation_context)
            prompt_template = ChatPromptTemplate.from_messages(
                [
                    ("system", system_prompt),
                    MessagesPlaceholder(variable_name="chat_history", optional=True),
                    ("human", "{input}"),
                    MessagesPlaceholder(variable_name="agent_scratchpad", optional=True),
                ]
            )

            # --- Crear agente ---
            agent_chain = create_openai_tools_agent(
                llm=self.llm, tools=tools, prompt=prompt_template
            )

            executor = AgentExecutor(
                agent=agent_chain,
                tools=tools,
                verbose=True,
                max_iterations=15,
                return_intermediate_steps=False,
                handle_parsing_errors=True,
                max_execution_time=60,
            )

            # --- Ejecutar agente ---
            result = await executor.ainvoke(
                input={"input": user_input, "chat_history": chat_history},
                config={"callbacks": []},
            )

            output = (result.get("output") or "").strip()
            if not output:
                output = "No se pudo procesar la solicitud."

            # --- Persistir en memoria ---
            await self._persist_interaction(
                chat_id=chat_id,
                user_input=user_input,
                output=output,
                escalation_id=escalation_id,
                notify_result=notify_result,
            )

            # --- Actualizar DB ---
            if self.escalation_db and escalation_id:
                await self._safe_call(
                    getattr(self.escalation_db, "update_escalation", None),
                    escalation_id=escalation_id,
                    updates={
                        "latest_message": user_input,
                        "latest_response": output,
                        "updated_at": datetime.utcnow().isoformat(),
                    },
                )

            return output

        except Exception as exc:
            log.error("Error en InternoAgent.ainvoke: %s", exc, exc_info=True)

            await self._safe_call(
                getattr(self.memory_manager, "save", None),
                conversation_id=chat_id,
                role="system",
                content=f"[InternoAgent-Error] {exc}",
            )
            raise

    async def _persist_interaction(
        self,
        *,
        chat_id: str,
        user_input: str,
        output: str,
        escalation_id: Optional[str],
        notify_result: Optional[str],
    ) -> None:

        await self._safe_call(
            getattr(self.memory_manager, "save", None),
            conversation_id=chat_id,
            role="user",
            content=user_input,
        )
        await self._safe_call(
            getattr(self.memory_manager, "save", None),
            conversation_id=chat_id,
            role="assistant",
            content=f"[InternoAgent] {output}",
        )

        if notify_result:
            await self._safe_call(
                getattr(self.memory_manager, "save", None),
                conversation_id=chat_id,
                role="system",
                content=f"[InternoAgent] Notificación enviada: {notify_result}",
            )

    async def handle_guest_escalation(
        self,
        chat_id: str,
        guest_message: str,
        reason: str,
        escalation_type: str = "info_not_found",
        context: str = "Auto-escalación",
        confirmation_flag: Optional[str] = None,
    ) -> str:

        if self.memory_manager:
            try:
                self.memory_manager.set_flag(chat_id, "escalation_in_progress", True)
                if confirmation_flag:
                    self.memory_manager.clear_flag(chat_id, confirmation_flag)
            except Exception:
                pass

        try:
            response = await self.escalate(
                guest_chat_id=chat_id,
                guest_message=guest_message,
                escalation_type=escalation_type,
                reason=reason,
                context=context,
            )
            self._schedule_flag_cleanup(chat_id)
            return response

        except Exception:
            if self.memory_manager:
                try:
                    self.memory_manager.clear_flag(chat_id, "escalation_in_progress")
                except Exception:
                    pass
            raise

    async def escalate(
        self,
        guest_chat_id: str,
        guest_message: str,
        escalation_type: str,
        reason: str,
        context: str,
    ) -> str:

        escalation_id = f"esc_{guest_chat_id}_{int(datetime.utcnow().timestamp())}"

        prompt = (
            "Nueva escalación:\n"
            f"- ID: {escalation_id}\n"
            f"- Chat ID: {guest_chat_id}\n"
            f"- Tipo: {escalation_type}\n"
            f"- Mensaje: {guest_message}\n"
            f"- Razón: {reason}\n"
            f"- Contexto: {context}\n\n"
            "Usa la tool 'notificar_encargado' con estos datos."
        )

        return await self.ainvoke(
            user_input=prompt,
            chat_id=guest_chat_id,
            escalation_id=escalation_id,
            escalation_context=f"MAIN_AUTO_{escalation_type.upper()}",
            context_window=0,
            chat_history=[],
        )

    async def process_manager_reply(
        self,
        escalation_id: str,
        manager_reply: str,
        chat_id: Optional[str] = None,
    ) -> str:

        chat_id = chat_id or self._resolve_guest_chat_id(escalation_id)
        return await self.ainvoke(
            user_input=f"Respuesta del encargado: {manager_reply}",
            chat_id=chat_id,
            escalation_id=escalation_id,
            escalation_context="HUMAN_RESPONSE",
            context_window=20,
        )

    async def send_confirmed_response(
        self,
        escalation_id: str,
        confirmed: bool = True,
        adjustments: str = "",
    ) -> str:

        chat_id = self._resolve_guest_chat_id(escalation_id)

        prompt = (
            f"Confirmación para la escalación {escalation_id}:\n"
            f"- Confirmado: {confirmed}\n"
            f"- Ajustes: {adjustments}\n\n"
            "Usa la tool 'confirmar_y_enviar_respuesta'."
        )

        return await self.ainvoke(
            user_input=prompt,
            chat_id=chat_id,
            escalation_id=escalation_id,
            escalation_context="HUMAN_CONFIRMATION",
            context_window=20,
        )

    def _build_system_prompt(self, escalation_context: str) -> str:
        base = load_prompt("interno_prompt.txt") or self._get_default_prompt()
        ctx = get_time_context()

        extra = ""
        c = escalation_context.upper()

        if "SUPERVISOR_INPUT" in c:
            extra = "\n\nCONTEXTO: Mensaje inapropiado detectado."
        elif "SUPERVISOR_OUTPUT" in c:
            extra = "\n\nCONTEXTO: Respuesta incoherente detectada."
        elif "MAIN_AUTO" in c:
            extra = "\n\nCONTEXTO: Escalación automática del MainAgent."
        elif "HUMAN_DIRECT" in c:
            extra = "\n\nCONTEXTO: El huésped pidió hablar con el encargado."

        return f"{ctx}\n{base}{extra}"

    def _get_default_prompt(self) -> str:
        return (
            "Eres el Agente Interno del Sistema de IA Hotelera.\n"
            "Coordinas entre encargado y huésped.\n\n"
            "IMPORTANTE:\n"
            "- NO incluyas razonamiento ni análisis en tu output\n"
            "- SOLO llama a las tools necesarias\n"
            "- El output de las tools ya está formateado para el encargado\n"
            "- NO agregues explicaciones adicionales\n\n"
            "Herramientas disponibles:\n"
            "- notificar_encargado: Envía la escalación al encargado\n"
            "- generar_borrador_respuesta: Crea borrador profesional para el huésped\n"
            "- confirmar_y_enviar_respuesta: Envía la respuesta final al huésped\n"
        )

    def _schedule_flag_cleanup(self, chat_id: str, delay: int = 90) -> None:
        if not self.memory_manager:
            return

        async def cleanup():
            await asyncio.sleep(delay)
            try:
                self.memory_manager.clear_flag(chat_id, "escalation_in_progress")
            except Exception:
                pass

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(cleanup())
        except RuntimeError:
            pass

    def _resolve_guest_chat_id(self, escalation_id: str) -> Optional[str]:

        esc = self.escalations.get(escalation_id)
        if esc:
            return esc.guest_chat_id

        try:
            from core.escalation_db import get_escalation
            record = get_escalation(escalation_id)
            if record:
                return record.get("guest_chat_id")
        except Exception:
            pass

        return None

    async def _safe_call(self, func: Optional[Any], *args, **kwargs):
        if not func:
            return None
        try:
            res = func(*args, **kwargs)
            if inspect.isawaitable(res):
                return await res
            return res
        except Exception as exc:
            log.warning("Error en llamada segura: %s", exc)
            raise
