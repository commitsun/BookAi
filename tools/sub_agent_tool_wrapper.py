"""Sub-Agent Tool Wrapper - Implementa patrón n8n toolWorkflow."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from inspect import iscoroutinefunction, signature
from typing import Any, Type

from langchain.tools import BaseTool
from pydantic import BaseModel, Field
from core.config import ModelConfig, ModelTier

log = logging.getLogger("SubAgentTool")


class SubAgentToolInput(BaseModel):
    """Schema de entrada para sub-agentes."""

    query: str | None = Field(
        default=None, description="Pregunta o tarea para el sub-agente"
    )
    pregunta: str | None = Field(
        default=None, description="Compat: pregunta del huésped"
    )
    mensaje_cliente: str | None = Field(
        default=None, description="Compat: mensaje original del cliente"
    )
    motivo: str | None = Field(
        default=None, description="Compat: motivo de la escalación"
    )
    tipo: str | None = Field(
        default=None, description="Compat: tipo de escalación"
    )


class SubAgentTool(BaseTool):
    """Wrapper que expone un sub-agente completo como herramienta."""

    name: str = "sub_agent_tool"
    description: str = "Sub-agente"

    sub_agent: Any
    memory_manager: Any
    chat_id: str
    hotel_name: str = ""

    args_schema: Type[BaseModel] = SubAgentToolInput

    class Config:
        arbitrary_types_allowed = True

    @staticmethod
    def _normalize_text(text: str) -> str:
        return " ".join(str(text or "").strip().lower().split())

    async def _is_query_aligned(self, canonical_question: str, tool_query: str) -> bool:
        canonical = self._normalize_text(canonical_question)
        query = self._normalize_text(tool_query)
        if not canonical or not query:
            return True
        if canonical == query:
            return True
        try:
            llm = ModelConfig.get_llm(ModelTier.SUBAGENT)
            system_prompt = (
                "Compara dos textos y decide si expresan la MISMA intención del huésped.\n"
                "Responde SOLO con: MATCH o MISMATCH."
            )
            user_prompt = (
                f"Pregunta canónica del huésped:\n{canonical_question}\n\n"
                f"Query que intenta usar la tool:\n{tool_query}\n\n"
                "¿Misma intención?"
            )
            raw = await llm.ainvoke(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ]
            )
            verdict = (getattr(raw, "content", None) or str(raw or "")).strip().upper()
            return verdict == "MATCH"
        except Exception:
            # En caso de fallo, no bloquear el flujo.
            return True

    async def _arun(
        self,
        query: str | None = None,
        pregunta: str | None = None,
        mensaje_cliente: str | None = None,
        motivo: str | None = None,
        tipo: str | None = None,
    ) -> str:
        try:
            # Prioriza siempre la pregunta completa del huésped para no perder contexto.
            effective_query = (pregunta or "").strip() or (query or "").strip() or (mensaje_cliente or "").strip()
            if not effective_query:
                raise ValueError("Falta 'query' o 'pregunta' para el sub-agente.")

            log.info("SubAgentTool._arun: %s - query: %s", self.name, effective_query[:50])

            chat_history = None
            if self.memory_manager and self.chat_id:
                try:
                    chat_history = self.memory_manager.get_memory_as_messages(
                        conversation_id=self.chat_id,
                        limit=20,
                    )
                except Exception as mm_err:
                    log.warning(
                        "No se pudo recuperar chat_history para %s: %s",
                        self.chat_id,
                        mm_err,
                    )

            if self.name == "base_conocimientos":
                canonical_question = (pregunta or mensaje_cliente or "").strip()
                rewritten_query = (query or "").strip()
                if canonical_question and rewritten_query:
                    aligned = await self._is_query_aligned(
                        canonical_question=canonical_question,
                        tool_query=rewritten_query,
                    )
                    if not aligned:
                        log.info(
                            "SubAgentTool(%s): query desalineada detectada en el turno actual. "
                            "Se corrige query='%s' -> pregunta='%s'",
                            self.name,
                            rewritten_query[:120],
                            canonical_question[:120],
                        )
                        effective_query = canonical_question

            # Caso 1: sub-agente es InternoAgent
            if hasattr(self.sub_agent, "handle_guest_escalation"):
                reason = motivo or "Consulta del huésped gestionada por MainAgent"
                escalation_type = tipo or "manual"
                result = await self.sub_agent.handle_guest_escalation(
                    chat_id=self.chat_id,
                    guest_message=effective_query,
                    reason=reason,
                    escalation_type=escalation_type,
                    context=(
                        f"Escalación manual desde MainAgent ({self.hotel_name})"
                        if self.hotel_name
                        else "Escalación manual desde MainAgent"
                    ),
                )

            # Caso 2: agentes simples tipo InfoAgent / DispoPreciosAgent
            elif hasattr(self.sub_agent, "handle"):
                result = await self.sub_agent.handle(
                    pregunta=effective_query,
                    chat_id=self.chat_id,
                    chat_history=chat_history,
                )

            # Caso 3: agente compatible con ainvoke → llama con kwargs dinámicos
            elif hasattr(self.sub_agent, "ainvoke"):
                invoke_kwargs = {
                    "user_input": effective_query,
                    "chat_id": self.chat_id,
                    "escalation_context": "MAIN_AGENT_TOOL",
                }

                try:
                    params = signature(self.sub_agent.ainvoke).parameters
                except (TypeError, ValueError):
                    params = {}

                if "chat_history" in params:
                    invoke_kwargs["chat_history"] = chat_history

                if "escalation_payload" in params or "auto_notify" in params:
                    raw_chat_id = str(self.chat_id or "").strip()
                    payload = {
                        "escalation_id": f"esc_{raw_chat_id}_{int(datetime.utcnow().timestamp())}",
                        "guest_chat_id": raw_chat_id,
                        "guest_message": effective_query,
                        "escalation_type": tipo or "manual",
                        "reason": motivo or "Solicitud del huésped desde MainAgent",
                        "context": (
                            f"Escalación manual desde MainAgent ({self.hotel_name})"
                            if self.hotel_name
                            else "Escalación manual desde MainAgent"
                        ),
                    }

                    if "escalation_payload" in params:
                        invoke_kwargs["escalation_payload"] = payload
                    if "auto_notify" in params:
                        invoke_kwargs["auto_notify"] = True

                result = await self.sub_agent.ainvoke(**invoke_kwargs)

            # Caso 4: fallback → método invoke
            elif hasattr(self.sub_agent, "invoke"):
                invoke_callable = getattr(self.sub_agent, "invoke")

                if iscoroutinefunction(invoke_callable):
                    result = await invoke_callable(
                        effective_query,
                        chat_history=chat_history,
                        chat_id=self.chat_id,
                    )
                else:
                    result = await asyncio.to_thread(
                        invoke_callable,
                        effective_query,
                        chat_history=chat_history,
                        chat_id=self.chat_id,
                    )
            else:
                raise ValueError(
                    f"Sub-agente {self.name} no tiene método compatible"
                )

            log.debug("SubAgentTool resultado: %s chars", len(str(result)))
            return str(result).strip()

        except Exception as exc:
            log.error(
                "Error en SubAgentTool %s: %s", self.name, exc, exc_info=True
            )
            return f"Error procesando consulta en {self.name}: {exc}"

    def _run(
        self,
        query: str | None = None,
        pregunta: str | None = None,
        mensaje_cliente: str | None = None,
        motivo: str | None = None,
        tipo: str | None = None,
    ) -> str:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        if loop.is_running():
            import nest_asyncio
            nest_asyncio.apply()

        try:
            return loop.run_until_complete(
                self._arun(
                    query=query,
                    pregunta=pregunta,
                    mensaje_cliente=mensaje_cliente,
                    motivo=motivo,
                    tipo=tipo,
                )
            )
        except Exception as exc:
            log.error("Error en SubAgentTool._run: %s", exc)
            return f"Error: {exc}"


def create_sub_agent_tool(
    name: str,
    description: str,
    sub_agent: Any,
    memory_manager: Any,
    chat_id: str,
    hotel_name: str = "",
) -> SubAgentTool:
    """Factory function para crear sub-agent tools."""

    return SubAgentTool(
        name=name,
        description=description,
        sub_agent=sub_agent,
        memory_manager=memory_manager,
        chat_id=chat_id,
        hotel_name=hotel_name,
    )
