"""Sub-Agent Tool Wrapper - Implementa patrón n8n toolWorkflow."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from inspect import iscoroutinefunction, signature
from typing import Any, Type

from langchain.tools import BaseTool
from pydantic import BaseModel, Field

log = logging.getLogger("SubAgentTool")


class SubAgentToolInput(BaseModel):
    """Schema de entrada para sub-agentes"""

    query: str = Field(
        ..., description="Pregunta o tarea para el sub-agente"
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

    async def _arun(self, query: str) -> str:
        try:
            log.info("SubAgentTool._arun: %s - query: %s", self.name, query[:50])

            # Caso 1: sub-agente es InternoAgent
            if hasattr(self.sub_agent, "handle_guest_escalation"):
                result = await self.sub_agent.handle_guest_escalation(
                    chat_id=self.chat_id,
                    guest_message=query,
                    reason="Consulta del huésped gestionada por MainAgent",
                    escalation_type="manual",
                    context=(
                        f"Escalación manual desde MainAgent ({self.hotel_name})"
                        if self.hotel_name
                        else "Escalación manual desde MainAgent"
                    ),
                )

            # Caso 2: agentes simples tipo InfoAgent / DispoPreciosAgent
            elif hasattr(self.sub_agent, "handle"):
                result = await self.sub_agent.handle(
                    pregunta=query,
                    chat_id=self.chat_id,
                    chat_history=None,
                )

            # Caso 3: agente compatible con ainvoke → llama con kwargs dinámicos
            elif hasattr(self.sub_agent, "ainvoke"):
                invoke_kwargs = {
                    "user_input": query,
                    "chat_id": self.chat_id,
                    "escalation_context": "MAIN_AGENT_TOOL",
                }

                try:
                    params = signature(self.sub_agent.ainvoke).parameters
                except (TypeError, ValueError):
                    params = {}

                if "escalation_payload" in params or "auto_notify" in params:
                    payload = {
                        "escalation_id": f"esc_{self.chat_id}_{int(datetime.utcnow().timestamp())}",
                        "guest_chat_id": self.chat_id,
                        "guest_message": query,
                        "escalation_type": "manual",
                        "reason": "Solicitud del huésped desde MainAgent",
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
                        query,
                        chat_history=None,
                        chat_id=self.chat_id,
                    )
                else:
                    result = await asyncio.to_thread(
                        invoke_callable,
                        query,
                        chat_history=None,
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

    def _run(self, query: str) -> str:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        if loop.is_running():
            import nest_asyncio
            nest_asyncio.apply()

        try:
            return loop.run_until_complete(self._arun(query))
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
