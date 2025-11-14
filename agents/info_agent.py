"""
📚 InfoAgent v4 — factual y sin invenciones
Responde preguntas generales sobre el hotel.
Usa la base de conocimiento (MCP) y escala al encargado si no hay información válida.
"""

import re
import logging
import asyncio

# Core imports
from core.language_manager import language_manager
from core.utils.normalize_reply import normalize_reply
from core.mcp_client import mcp_client
from core.utils.time_context import get_time_context
from core.utils.utils_prompt import load_prompt
from core.config import ModelConfig, ModelTier  # ✅ configuración centralizada

log = logging.getLogger("InfoAgent")


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
            return "ESCALATION_REQUIRED"

        tools = await mcp_client.get_tools(server_name="InfoAgent")
        if not tools:
            log.warning("⚠️ No se encontraron herramientas MCP para InfoAgent.")
            return "ESCALATION_REQUIRED"

        info_tool = next((t for t in tools if "conocimiento" in t.name.lower()), None)
        if not info_tool:
            log.warning("⚠️ No se encontró 'Base_de_conocimientos_del_hotel' en MCP.")
            return "ESCALATION_REQUIRED"

        raw_reply = await info_tool.ainvoke({"input": q})
        cleaned = normalize_reply(raw_reply, q, "InfoAgent").strip()

        if not cleaned or len(cleaned) < 10:
            return "ESCALATION_REQUIRED"
        if _looks_like_internal_dump(cleaned):
            return "ESCALATION_REQUIRED"
        if "no hay resultados" in cleaned.lower() or "no encontrado" in cleaned.lower():
            return "ESCALATION_REQUIRED"

        cleaned = re.sub(r"[*#>\-]+", "", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        return cleaned

    except Exception as e:
        log.error(f"❌ Error en hotel_information_tool: {e}", exc_info=True)
        return "ESCALATION_REQUIRED"


# =============================================================
# 🧠 InfoAgent — factual, solicita confirmación de escalación
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
        self.memory_manager = memory_manager

        if model_name or temperature:
            from langchain_openai import ChatOpenAI
            name = model_name or "gpt-4.1"
            temp = temperature if temperature is not None else 0.3
            self.llm = ChatOpenAI(model=name, temperature=temp)
        else:
            self.llm = ModelConfig.get_llm(ModelTier.SUBAGENT)

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
            respuesta_final = await hotel_information_tool(user_input)

            if respuesta_final == "ESCALATION_REQUIRED":
                log.warning("⚠️ La base MCP no devolvió información suficiente. Se solicitará confirmación al huésped.")
                if self.memory_manager and chat_id:
                    self.memory_manager.update_memory(
                        chat_id,
                        role="system",
                        content=(
                            "[InfoAgent] Base de conocimiento sin datos útiles. "
                            "Se recomienda confirmar escalación con el encargado."
                        ),
                    )
                return "ESCALATION_REQUIRED"

            respuesta_final = language_manager.ensure_language(respuesta_final, lang)

            if self.memory_manager and chat_id:
                self.memory_manager.update_memory(
                    chat_id,
                    role="assistant",
                    content=f"[InfoAgent] Entrada: {user_input}\n\nRespuesta factual: {respuesta_final}"
                )

            lower_response = respuesta_final.lower()
            no_info = any(
                p in lower_response
                for p in [
                    "no dispongo",
                    "no disponemos",
                    "no tengo información",
                    "consultarlo con el encargado",
                    "permíteme contactar",
                    "no hay resultados",
                    "no encontrado",
                    "¿te gustaría consultar por algún otro servicio",
                    "te gustaría consultar por algún otro servicio",
                    "te gustaria consultar por algun otro servicio",
                ]
            )

            if _looks_like_internal_dump(respuesta_final) or no_info:
                log.warning("⚠️ Respuesta ambigua o insuficiente. Se solicitará confirmación de escalación.")
                if self.memory_manager and chat_id:
                    self.memory_manager.update_memory(
                        chat_id,
                        role="system",
                        content=(
                            "[InfoAgent] Respuesta insuficiente en MCP. "
                            "Sugerir confirmación con el encargado."
                        ),
                    )
                return "ESCALATION_REQUIRED"

            log.info(f"✅ [InfoAgent] Respuesta factual: {respuesta_final[:200]}")
            return respuesta_final or "ESCALATION_REQUIRED"

        except Exception as e:
            log.error(f"💥 Error en InfoAgent.invoke: {e}", exc_info=True)
            if self.memory_manager and chat_id:
                self.memory_manager.update_memory(
                    chat_id, role="system",
                    content=f"[InfoAgent] Error interno: {e}"
                )
            return "ESCALATION_REQUIRED"
