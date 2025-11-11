"""
📚 InfoAgent v4 — factual y sin invenciones
Responde preguntas generales sobre el hotel.
Usa la base de conocimiento (MCP) y escala al encargado si no hay información válida.
"""

import re
import logging
import asyncio
from langchain.tools import Tool

# Core imports
from core.language_manager import language_manager
from core.utils.normalize_reply import normalize_reply
from core.mcp_client import mcp_client
from core.utils.time_context import get_time_context
from core.utils.utils_prompt import load_prompt
from core.config import ModelConfig, ModelTier  # ✅ configuración centralizada

# Interno Agent (para escalaciones)
from agents.interno_agent import InternoAgent

log = logging.getLogger("InfoAgent")

ESCALATE_SENTENCE = (
    "🕓 Un momento por favor, voy a consultarlo con el encargado. "
    "Permíteme contactar con el encargado."
)


# =============================================================
# 🔍 Detección de dumps técnicos o respuestas anómalas
# =============================================================
def _looks_like_internal_dump(text: str) -> bool:
    """Detecta texto técnico interno o volcado anómalo."""
    if not text:
        return False

    dump_patterns = ["traceback", "error", "exception", "{", "}", "SELECT ", "sql", "schema"]
    if any(pat.lower() in text.lower() for pat in dump_patterns):
        return True

    keywords_ok = [
        "gimnasio", "desayuno", "recepción", "parking",
        "mascotas", "wifi", "check-in", "restaurante",
        "habitaciones", "coworking", "lavandería"
    ]
    if len(text.split()) > 1200 and not any(k in text.lower() for k in keywords_ok):
        return True

    return False


# =============================================================
# 🧩 Tool principal — consulta factual a MCP
# =============================================================
async def hotel_information_tool(query: str) -> str:
    """Consulta factual desde la base de conocimiento (MCP)."""
    try:
        q = (query or "").strip()
        if not q:
            return ESCALATE_SENTENCE

        tools = await mcp_client.get_tools(server_name="InfoAgent")
        if not tools:
            log.warning("⚠️ No se encontraron herramientas MCP para InfoAgent.")
            return ESCALATE_SENTENCE

        info_tool = next((t for t in tools if "conocimiento" in t.name.lower()), None)
        if not info_tool:
            log.warning("⚠️ No se encontró 'Base_de_conocimientos_del_hotel' en MCP.")
            return ESCALATE_SENTENCE

        raw_reply = await info_tool.ainvoke({"input": q})
        cleaned = normalize_reply(raw_reply, q, "InfoAgent").strip()

        if not cleaned or len(cleaned) < 10:
            return ESCALATE_SENTENCE
        if _looks_like_internal_dump(cleaned):
            return ESCALATE_SENTENCE
        if "no hay resultados" in cleaned.lower() or "no encontrado" in cleaned.lower():
            return ESCALATE_SENTENCE

        cleaned = re.sub(r"[*#>\-]+", "", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        return cleaned

    except Exception as e:
        log.error(f"❌ Error en hotel_information_tool: {e}", exc_info=True)
        return ESCALATE_SENTENCE


# =============================================================
# 🧠 InfoAgent — factual, con escalación automática
# =============================================================
class InfoAgent:
    """Agente factual — usa ModelConfig y prompt de utils_prompt."""

    def __init__(self, memory_manager=None, model_name=None, temperature=None):
        """
        Args:
            memory_manager: Gestor de memoria contextual.
            model_name: (opcional) Modelo a usar. Si no se pasa, se toma del ModelConfig centralizado.
            temperature: (opcional) Temperatura del modelo.
        """
        # ✅ Compatibilidad con llamadas antiguas y nuevas
        self.memory_manager = memory_manager

        if model_name or temperature:
            # Si se especifica explícitamente, crear un LLM con esos valores
            from langchain_openai import ChatOpenAI
            name = model_name or "gpt-4.1"
            temp = temperature if temperature is not None else 0.3
            self.llm = ChatOpenAI(model=name, temperature=temp)
        else:
            # Si no se pasa, usar el modelo centralizado (config.py)
            self.llm = ModelConfig.get_llm(ModelTier.SUBAGENT)

        self.interno_agent = InternoAgent(memory_manager=memory_manager)

        # 🧩 Prompt factual (importado con fallback)
        base_prompt = load_prompt("info_hotel_prompt.txt") or (
            "Eres un agente de información del hotel. "
            "Responde solo con datos verificables de la base MCP y escala al encargado si no tienes información."
        )
        self.prompt_text = f"{get_time_context()}\n\n{base_prompt.strip()}"

        log.info(f"✅ InfoAgent inicializado (modelo={self.llm.model_name})")

    # --------------------------------------------------
    def _sync_run(self, coro, *args, **kwargs):
        """Ejecuta async dentro de sync context (para compatibilidad LangChain)."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        if loop.is_running():
            import nest_asyncio
            nest_asyncio.apply()
        return loop.run_until_complete(coro(*args, **kwargs))

    # --------------------------------------------------
    async def invoke(self, user_input: str, chat_history: list = None, chat_id: str = None) -> str:
        """Responde consultas factuales del huésped."""
        log.info(f"📩 [InfoAgent] Consulta: {user_input}")
        lang = language_manager.detect_language(user_input)
        chat_history = chat_history or []

        try:
            # 🧩 Consultar la base MCP
            respuesta_final = await hotel_information_tool(user_input)
            respuesta_final = language_manager.ensure_language(respuesta_final, lang)

            # 💾 Guardar memoria contextual
            if self.memory_manager and chat_id:
                self.memory_manager.update_memory(
                    chat_id,
                    role="assistant",
                    content=f"[InfoAgent] Entrada: {user_input}\n\nRespuesta factual: {respuesta_final}"
                )

            # 🚨 Detección de falta de información
            no_info = any(
                p in respuesta_final.lower()
                for p in [
                    "no dispongo", "no tengo información", "consultarlo con el encargado",
                    "permíteme contactar", "no hay resultados", "no encontrado"
                ]
            )

            if _looks_like_internal_dump(respuesta_final) or no_info or respuesta_final == ESCALATE_SENTENCE:
                log.warning("⚠️ Escalación automática por falta de información factual.")
                await self.interno_agent.escalate(
                    guest_chat_id=chat_id,
                    guest_message=user_input,
                    escalation_type="info_no_encontrada",
                    reason="Falta de información factual en la base de conocimiento.",
                    context="Escalación automática desde InfoAgent (factual)"
                )
                return language_manager.ensure_language(ESCALATE_SENTENCE, lang)

            log.info(f"✅ [InfoAgent] Respuesta factual: {respuesta_final[:200]}")
            return respuesta_final or ESCALATE_SENTENCE

        except Exception as e:
            log.error(f"💥 Error en InfoAgent.invoke: {e}", exc_info=True)
            if self.memory_manager and chat_id:
                self.memory_manager.update_memory(
                    chat_id, role="system",
                    content=f"[InfoAgent] Error interno: {e}"
                )

            await self.interno_agent.escalate(
                guest_chat_id=chat_id,
                guest_message=user_input,
                escalation_type="error_runtime",
                reason="Error interno en InfoAgent",
                context="Fallo durante procesamiento"
            )
            return language_manager.ensure_language(ESCALATE_SENTENCE, lang)
