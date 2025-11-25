"""InfoAgent v5 — AgentExecutor con KB + Google Search.

Este módulo implementa la arquitectura propuesta en la incidencia:
- Tool 1: Base de conocimientos (MCP)
- Tool 2: Búsqueda en Google (placeholder)
El LLM decide qué herramienta usar y solo escala si ambas fallan.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import ClassVar, List, Optional

from langchain.agents import AgentExecutor, create_openai_tools_agent
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain.tools import BaseTool
from pydantic import BaseModel, Field

from core.config import ModelConfig, ModelTier
from core.mcp_client import mcp_client
from core.utils.normalize_reply import normalize_reply
from core.utils.time_context import get_time_context
from core.utils.utils_prompt import load_prompt

log = logging.getLogger("InfoAgent")

ESCALATION_TOKEN = "ESCALATION_REQUIRED"


async def _invoke_google_search(query: str) -> Optional[str]:
    """
    Consulta la herramienta `google` expuesta por el MCP para InfoAgent.
    Devuelve el texto limpio o None si no hay resultados útiles.
    """
    question = (query or "").strip()
    if not question:
        return None

    try:
        tools = await mcp_client.get_tools(server_name="InfoAgent")
        google_tool = next((t for t in tools if "google" in t.name.lower()), None)
        if not google_tool:
            log.warning("GoogleSearchTool: no se encontró la tool 'google' en el MCP.")
            return None

        raw_reply = await google_tool.ainvoke({"query": question})
        cleaned = normalize_reply(raw_reply, question, "InfoAgent").strip()
        if not cleaned or len(cleaned) < 5:
            return None
        return cleaned

    except Exception as exc:
        log.error("GoogleSearchTool: error consultando MCP: %s", exc, exc_info=True)
        return None


class GoogleSearchInput(BaseModel):
    """Schema para búsqueda en Google."""

    query: str = Field(
        ...,
        description="Consulta a buscar en Google (máximo 100 caracteres).",
        max_length=100,
    )


class GoogleSearchTool(BaseTool):
    """Tool que realiza búsquedas en Google usando un placeholder."""

    name: ClassVar[str] = "google_search"
    description: ClassVar[str] = (
        "Busca información en Google usando Gemini API. Úsalo cuando la base "
        "de conocimientos no tenga respuesta o la información sea insuficiente."
    )
    args_schema: ClassVar[type[BaseModel]] = GoogleSearchInput

    async def _arun(self, query: str) -> str:
        query = (query or "").strip()
        if not query:
            return "Necesito una consulta para buscar en Google."

        try:
            log.info("GoogleSearchTool: consultando MCP para %s", query[:80])
            result_text = await _invoke_google_search(query)
            if not result_text:
                return "Google Search no devolvió resultados útiles."
            return result_text
        except Exception as exc:
            log.error("Error en GoogleSearchTool: %s", exc, exc_info=True)
            return f"Error buscando en Google: {exc}"

    def _run(self, query: str) -> str:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(self._arun(query))


class KBSearchInput(BaseModel):
    query: str = Field(
        ...,
        description="Consulta sobre servicios, políticas, horarios o ubicación del hotel.",
    )


class KBSearchTool(BaseTool):
    """Tool que consulta la base de conocimientos (MCP)."""

    name: ClassVar[str] = "base_conocimientos"
    description: ClassVar[str] = (
        "Busca información en la base de conocimientos del hotel. "
        "Úsalo siempre antes de intentar otras opciones."
    )
    args_schema: ClassVar[type[BaseModel]] = KBSearchInput

    async def _arun(self, query: str) -> Optional[str]:
        question = (query or "").strip()
        if not question:
            return "Por favor, formula una pregunta concreta."

        def _extract_focus_term(text: str) -> Optional[str]:
            words = re.findall(r"[a-záéíóúüñ]+", text.lower())
            if not words:
                return None
            # Evitar palabras muy genéricas
            stop = {"el", "la", "los", "las", "un", "una", "de", "del", "que", "es", "hay", "tiene"}
            focus = [w for w in words if w not in stop]
            return focus[-1] if focus else words[-1]

        try:
            tools = await mcp_client.get_tools(server_name="InfoAgent")
            if not tools:
                log.warning("KBSearchTool: no hay herramientas MCP disponibles.")
                return None

            kb_tool = next((t for t in tools if "conocimiento" in t.name.lower()), None)
            if not kb_tool:
                log.warning("KBSearchTool: no se encontró la herramienta de conocimientos.")
                return None

            raw_reply = await kb_tool.ainvoke({"input": question})
            def _is_invalid(text: str) -> bool:
                if not text or len(text) < 10:
                    return True
                lowered = text.lower()
                error_tokens = ["traceback", "error", "exception", "select", "schema"]
                if any(tok in lowered for tok in error_tokens):
                    return True
                no_info_tokens = [
                    "no dispongo",
                    "no tengo información",
                    "no hay resultados",
                    "no encontrado",
                    "no se encontró",
                ]
                if any(tok in lowered for tok in no_info_tokens):
                    return True
                return False

            cleaned = normalize_reply(raw_reply, question, "InfoAgent").strip()
            fallback_needed = _is_invalid(cleaned) or "no dispone" in cleaned.lower() or "no cuenta con" in cleaned.lower()

            if fallback_needed:
                focus = _extract_focus_term(question)
                if focus and focus != question.strip().lower():
                    log.info("KBSearchTool: reintentando KB con término focal '%s'", focus)
                    raw_retry = await kb_tool.ainvoke({"input": focus})
                    cleaned_retry = normalize_reply(raw_retry, focus, "InfoAgent").strip()
                    if not _is_invalid(cleaned_retry):
                        log.info("KBSearchTool: información obtenida correctamente (reintento).")
                        return cleaned_retry
                if _is_invalid(cleaned):
                    log.info("KBSearchTool: KB no tiene información útil tras reintento.")
                    return None

            log.info("KBSearchTool: información obtenida correctamente.")
            return cleaned
        except Exception as exc:
            log.error("KBSearchTool error: %s", exc, exc_info=True)
            return None

    def _run(self, query: str) -> Optional[str]:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(self._arun(query))


class InfoAgent:
    """Agente factual basado en AgentExecutor con múltiples herramientas."""

    def __init__(self, memory_manager=None):
        self.memory_manager = memory_manager
        self.llm = ModelConfig.get_llm(ModelTier.SUBAGENT)
        self.tools: List[BaseTool] = [KBSearchTool(), GoogleSearchTool()]

        base_prompt = _load_info_prompt()
        self.base_prompt = base_prompt or (
            "Eres un agente especializado en información del hotel. "
            "Responde solo con datos verificados y evita inventar."
        )

        log.info("InfoAgent v5 inicializado con AgentExecutor y herramientas múltiples.")

    async def ainvoke(
        self,
        user_input: str,
        chat_id: str,
        chat_history: Optional[List] = None,
        context_window: int = 10,
    ) -> str:
        if not user_input:
            return ESCALATION_TOKEN

        chat_history = chat_history or []
        if not chat_history and self.memory_manager and chat_id:
            try:
                chat_history = self.memory_manager.get_memory_as_messages(
                    conversation_id=chat_id,
                    limit=context_window,
                )
            except Exception as exc:
                log.warning("No se pudo recuperar historial para InfoAgent: %s", exc)
                chat_history = []

        system_prompt = (
            f"{get_time_context()}\n\n{self.base_prompt}\n\n"
            "ORDEN ESTRICTO DE HERRAMIENTAS:\n"
            "1) Usa 'base_conocimientos' para consultar la KB del hotel.\n"
            "2) Si no hay respuesta, usa 'google_search' para buscar en la web.\n"
            "3) Solo si ambas fallan, indica claramente que necesitas escalar.\n\n"
            "Instrucciones adicionales:\n"
            "- No inventes datos.\n"
            "- Aclara la fuente (KB vs Google).\n"
            "- Si sigues sin datos tras ambos intentos, di que necesitas escalar."
        )

        prompt_template = ChatPromptTemplate.from_messages(
            [
                ("system", system_prompt),
                MessagesPlaceholder(variable_name="chat_history", optional=True),
                ("human", "{input}"),
                MessagesPlaceholder(variable_name="agent_scratchpad", optional=True),
            ]
        )

        agent = create_openai_tools_agent(
            llm=self.llm,
            tools=self.tools,
            prompt=prompt_template,
        )

        executor = AgentExecutor(
            agent=agent,
            tools=self.tools,
            verbose=True,
            max_iterations=15,
            handle_parsing_errors=True,
            max_execution_time=60,
        )

        try:
            result = await executor.ainvoke(
                {
                    "input": user_input,
                    "chat_history": chat_history,
                }
            )
            output = (result.get("output") or "").strip()


        except Exception as exc:
            log.error("Error ejecutando InfoAgent: %s", exc, exc_info=True)
            return f"Error consultando la información del hotel: {exc}"

        if self.memory_manager and chat_id:
            try:
                self.memory_manager.save(chat_id, "user", user_input)
                if output:
                    self.memory_manager.save(chat_id, "assistant", f"[InfoAgent] {output}")
            except Exception as exc:
                log.warning("No se pudo guardar memoria en InfoAgent: %s", exc)

        if not output or self._needs_escalation(output):
            return ESCALATION_TOKEN

        return output

    async def handle(self, pregunta: str, chat_id: str, chat_history=None, **_) -> str:
        return await self.ainvoke(
            user_input=pregunta,
            chat_id=chat_id,
            chat_history=chat_history,
        )

    @staticmethod
    def _needs_escalation(text: str) -> bool:
        lowered = text.lower()
        triggers = [
            "no tengo información",
            "no dispongo",
            "consultar al encargado",
            "necesito escalar",
            "no puedo confirmarlo",
            "escalar al encargado",
        ]
        return any(token in lowered for token in triggers)


def _load_info_prompt() -> Optional[str]:
    try:
        return load_prompt("info_hotel_prompt.txt").strip()
    except FileNotFoundError:
        return None
    except Exception as exc:
        log.warning("No se pudo cargar info_hotel_prompt: %s", exc)
        return None
