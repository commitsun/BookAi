"""
OnboardingAgent - Consulta reservas existentes del huésped.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from langchain.agents import AgentExecutor, create_openai_tools_agent
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder

from core.config import ModelConfig, ModelTier, Settings
from core.utils.time_context import get_time_context
from core.utils.utils_prompt import load_prompt
from core.utils.dynamic_context import build_dynamic_context_from_memory
from tools.onboarding_tool import (
    create_room_type_tool,
    create_reservation_tool,
    create_token_tool,
    create_consulta_reserva_propia_tool,
)
from tools.superintendente_tool import create_consulta_reserva_persona_tool

log = logging.getLogger("OnboardingAgent")


class OnboardingAgent:
    """Agente para gestionar reservas iniciales via MCP."""

    _DEFAULT_PROMPT = (
        "Eres el agente de onboarding para consultar reservas del huésped.\n"
        "- NO puedes crear ni formalizar reservas nuevas.\n"
        "- Si el huésped pide una reserva nueva, indica claramente que ahora solo puedes consultar reservas ya existentes.\n"
        "- Si el huésped pide consultar su reserva, solicita el folio_id o localizador y usa consultar_reserva_propia.\n"
        "- Si el huésped pregunta por sus reservas, usa consultar_reserva_propia con listar=true.\n"
        "- Si hay folio_id en contexto, puedes usar consulta_reserva_persona para detalle.\n"
        "- Nunca muestres folio_id al huésped salvo que lo haya pedido explícitamente.\n"
        "- Responde breve y clara en el idioma del huésped.\n"
    )

    def __init__(self, memory_manager: Any = None):
        self.memory_manager = memory_manager
        self.llm = ModelConfig.get_llm(ModelTier.SUBAGENT)
        self.allow_reservation_creation = Settings.ONBOARDING_RESERVATION_CREATION_ENABLED
        self.prompt_text = self._build_prompt()
        log.info(
            "OnboardingAgent inicializado (modelo: %s, create_enabled=%s)",
            self.llm.model_name,
            self.allow_reservation_creation,
        )

    def _build_prompt(self) -> str:
        base_prompt = load_prompt("onboarding_prompt.txt") or self._DEFAULT_PROMPT
        return f"{get_time_context()}\n{base_prompt.strip()}"

    def _build_executor(self, tools) -> AgentExecutor:
        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", self.prompt_text),
                MessagesPlaceholder(variable_name="chat_history", optional=True),
                ("human", "{input}"),
                MessagesPlaceholder(variable_name="agent_scratchpad"),
            ]
        )
        agent = create_openai_tools_agent(self.llm, tools, prompt)
        return AgentExecutor(
            agent=agent,
            tools=tools,
            verbose=True,
            return_intermediate_steps=False,
            max_iterations=6,
            max_execution_time=90,
            handle_parsing_errors=True,
        )

    async def handle(
        self,
        pregunta: str,
        chat_id: str,
        chat_history: Optional[list[Any]] = None,
    ) -> str:
        """Punto de entrada para SubAgentTool."""
        base_prompt = load_prompt("onboarding_prompt.txt") or self._DEFAULT_PROMPT
        dynamic_context = build_dynamic_context_from_memory(self.memory_manager, chat_id)
        if dynamic_context:
            self.prompt_text = f"{get_time_context()}\n{base_prompt.strip()}\n\n{dynamic_context}"
        else:
            self.prompt_text = f"{get_time_context()}\n{base_prompt.strip()}"
        tools = []
        if self.allow_reservation_creation:
            tools.extend(
                [
                    create_token_tool(),
                    create_room_type_tool(memory_manager=self.memory_manager, chat_id=chat_id),
                    create_reservation_tool(memory_manager=self.memory_manager, chat_id=chat_id),
                ]
            )
        tools.extend(
            [
                create_consulta_reserva_propia_tool(
                    memory_manager=self.memory_manager,
                    chat_id=chat_id,
                ),
                create_consulta_reserva_persona_tool(
                    memory_manager=self.memory_manager,
                    chat_id=chat_id,
                ),
            ]
        )
        executor = self._build_executor(tools)
        result = await executor.ainvoke(
            input={
                "input": pregunta,
                "chat_history": chat_history or [],
            }
        )
        output = (result.get("output") or "").strip()
        return output
