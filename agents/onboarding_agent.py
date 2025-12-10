"""
OnboardingAgent - Gestiona reservas completas usando MCP (token -> tipo -> reserva).

Se apoya en las tools expuestas por n8n a traves de MCP:
- buscar_token
- tipo_de_habitacion
- reserva
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from langchain.agents import AgentExecutor, create_openai_tools_agent
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder

from core.config import ModelConfig, ModelTier
from core.utils.time_context import get_time_context
from core.utils.utils_prompt import load_prompt
from tools.onboarding_tool import (
    create_room_type_tool,
    create_reservation_tool,
    create_token_tool,
)

log = logging.getLogger("OnboardingAgent")


class OnboardingAgent:
    """Agente para gestionar reservas iniciales via MCP."""

    def __init__(self, memory_manager: Any = None):
        self.memory_manager = memory_manager
        self.llm = ModelConfig.get_llm(ModelTier.SUBAGENT)
        self.prompt_text = self._build_prompt()
        log.info("OnboardingAgent inicializado (modelo: %s)", self.llm.model_name)

    def _build_prompt(self) -> str:
        base_prompt = load_prompt("onboarding_prompt.txt") or (
            "Eres el agente de onboarding para crear reservas de hotel.\n"
            "- Usa siempre las tools disponibles (token -> tipo de habitacion -> reserva) en ese orden logico.\n"
            "- Pide al huesped solo los datos faltantes: fechas (checkin/checkout), adultos/ninos, tipo de habitacion o preferencia, nombre, email y telefono.\n"
            "- Una vez tengas los datos, llama a crear_reserva_onboarding. Nunca inventes.\n"
            "- Si falta roomTypeId, llama primero a listar_tipos_habitacion y elige el id mas cercano al nombre solicitado.\n"
            "- Responde de forma clara y breve en el idioma que use el huesped. No multipliques ni recalcules importes (los da el PMS).\n"
        )
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
        tools = [
            create_token_tool(),
            create_room_type_tool(),
            create_reservation_tool(memory_manager=self.memory_manager, chat_id=chat_id),
        ]
        executor = self._build_executor(tools)
        result = await executor.ainvoke(
            input={
                "input": pregunta,
                "chat_history": chat_history or [],
            }
        )
        output = (result.get("output") or "").strip()
        return output
